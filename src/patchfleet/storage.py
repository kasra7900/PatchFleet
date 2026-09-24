"""SQLite-authoritative local state with a repairable JSONL event mirror."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Self

from .contracts import (
    ApprovalRecord,
    ApprovalType,
    Plan,
    RunRecord,
    RunState,
    canonical_plan_json,
    plan_fingerprint,
)
from .events import Event, EventType, event_json
from .state import transition


class StoreError(ValueError):
    """A storage operation violates a control-plane invariant."""


class ApprovalError(StoreError):
    """An approval does not match the current run and approval gate."""


class EventLogCorruption(StoreError):
    """A completed JSONL record disagrees with authoritative SQLite data."""


class SQLiteStore:
    """Single-process local store. Every mutation commits its event in SQLite."""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else Path.cwd() / ".patchfleet"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_directory = self.directory / "events"
        self.events_directory.mkdir(exist_ok=True)
        self.database_path = self.directory / "patchfleet.sqlite3"
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.database_path, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self.last_mirror_error: str | None = None
        try:
            self._create_schema()
            self.reconcile_events()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (plan_id, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                plan_fingerprint TEXT NOT NULL,
                plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
                state TEXT NOT NULL,
                reviewed_result_fingerprint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (plan_id, plan_fingerprint) REFERENCES plans(plan_id, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                approval_type TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
                subject_fingerprint TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _save_plan_unlocked(self, plan: Plan) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO plans (plan_id, fingerprint, plan_json, created_at) VALUES (?, ?, ?, ?)",
            (
                plan.plan_id,
                plan_fingerprint(plan),
                canonical_plan_json(plan),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _run_unlocked(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run ID: {run_id}")
        return RunRecord.model_validate(dict(row))

    def _plan_unlocked(self, run: RunRecord) -> Plan:
        row = self._connection.execute(
            "SELECT plan_json FROM plans WHERE plan_id = ? AND fingerprint = ?",
            (run.plan_id, run.plan_fingerprint),
        ).fetchone()
        if row is None:
            raise StoreError("run refers to a missing plan version")
        return Plan.model_validate_json(row["plan_json"])

    def _approvals_unlocked(self, run_id: str) -> list[ApprovalRecord]:
        rows = self._connection.execute(
            "SELECT approval_id, run_id, approval_type, plan_id, plan_revision, subject_fingerprint, "
            "timestamp, actor, decision, reason FROM approvals WHERE run_id = ? ORDER BY approval_sequence",
            (run_id,),
        ).fetchall()
        return [ApprovalRecord.model_validate(dict(row)) for row in rows]

    def _events_unlocked(self, run_id: str) -> list[Event]:
        rows = self._connection.execute(
            "SELECT run_id, sequence, timestamp, event_type, payload_json "
            "FROM events WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return [
            Event.model_validate(
                {
                    "run_id": row["run_id"],
                    "sequence": row["sequence"],
                    "timestamp": row["timestamp"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
            for row in rows
        ]

    def _insert_event_unlocked(
        self, run_id: str, event_type: EventType, payload: dict[str, str | int]
    ) -> None:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        event = Event(
            run_id=run_id,
            sequence=row["next_sequence"],
            timestamp=datetime.now(UTC),
            event_type=event_type,
            payload=payload,
        )
        self._connection.execute(
            "INSERT INTO events (run_id, sequence, timestamp, event_type, payload_json) VALUES (?, ?, ?, ?, ?)",
            (
                event.run_id,
                event.sequence,
                event.timestamp.isoformat(),
                event.event_type.value,
                json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _write_run_unlocked(self, run: RunRecord) -> None:
        self._connection.execute(
            "UPDATE runs SET plan_fingerprint = ?, plan_revision = ?, state = ?, "
            "reviewed_result_fingerprint = ?, updated_at = ? WHERE run_id = ?",
            (
                run.plan_fingerprint,
                run.plan_revision,
                run.state.value,
                run.reviewed_result_fingerprint,
                run.updated_at.isoformat(),
                run.run_id,
            ),
        )

    def _mirror_run_unlocked(self, run_id: str) -> None:
        expected = self._events_unlocked(run_id)
        path = self.events_directory / f"{run_id}.jsonl"
        with path.open("a+b") as log:
            log.seek(0)
            data = log.read()
            complete_end = data.rfind(b"\n") + 1
            if complete_end < len(data):
                # Only an incomplete trailing write is removed. Complete records
                # are immutable and must agree with authoritative SQLite rows.
                log.truncate(complete_end)
                data = data[:complete_end]
            lines = data.splitlines()
            if len(lines) > len(expected):
                raise EventLogCorruption(f"JSONL has events absent from SQLite for run {run_id}")
            for index, line in enumerate(lines):
                if line != event_json(expected[index]).encode("utf-8"):
                    raise EventLogCorruption(
                        f"JSONL disagrees with SQLite at run {run_id} sequence {index + 1}"
                    )
            log.seek(0, os.SEEK_END)
            for event in expected[len(lines) :]:
                log.write((event_json(event) + "\n").encode("utf-8"))
            log.flush()
            os.fsync(log.fileno())

    def _mirror_after_commit_unlocked(self, run_id: str) -> None:
        try:
            self._mirror_run_unlocked(run_id)
            self.last_mirror_error = None
        except OSError as error:
            # SQLite already committed. The next startup can fill the missing
            # JSONL suffix; reporting a rollback here would be misleading.
            self.last_mirror_error = str(error)

    def reconcile_events(self) -> None:
        """Restore missing JSONL suffixes from SQLite on startup or on demand."""
        with self._lock:
            rows = self._connection.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall()
            for row in rows:
                self._mirror_run_unlocked(row["run_id"])
            self.last_mirror_error = None

    def create_run(self, run_id: str, plan: Plan) -> RunRecord:
        """Persist a draft run; validation remains an explicit later transition."""
        if not isinstance(plan, Plan):
            raise TypeError("plan must be a Plan")
        now = datetime.now(UTC)
        run = RunRecord(
            run_id=run_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan_fingerprint(plan),
            plan_revision=1,
            state=RunState.DRAFT,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            with self._transaction():
                self._save_plan_unlocked(plan)
                self._connection.execute(
                    "INSERT INTO runs (run_id, plan_id, plan_fingerprint, plan_revision, state, "
                    "reviewed_result_fingerprint, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run.run_id,
                        run.plan_id,
                        run.plan_fingerprint,
                        run.plan_revision,
                        run.state.value,
                        None,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                    ),
                )
                self._insert_event_unlocked(
                    run_id,
                    EventType.RUN_CREATED,
                    {
                        "plan_fingerprint": run.plan_fingerprint,
                        "plan_revision": 1,
                        "state": run.state.value,
                    },
                )
            self._mirror_after_commit_unlocked(run_id)
        return run

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            return self._run_unlocked(run_id)

    def get_plan(self, run_id: str) -> Plan:
        with self._lock:
            return self._plan_unlocked(self._run_unlocked(run_id))

    def get_approvals(self, run_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return self._approvals_unlocked(run_id)

    def get_events(self, run_id: str) -> list[Event]:
        with self._lock:
            self._run_unlocked(run_id)
            return self._events_unlocked(run_id)

    def transition_run(self, run_id: str, target: RunState) -> RunRecord:
        """Persist one guarded state transition and its ordered event atomically."""
        with self._lock:
            with self._transaction():
                current = self._run_unlocked(run_id)
                updated = transition(
                    current,
                    target,
                    plan=self._plan_unlocked(current),
                    approvals=self._approvals_unlocked(run_id),
                )
                self._write_run_unlocked(updated)
                self._insert_event_unlocked(
                    run_id,
                    EventType.STATE_TRANSITION,
                    {
                        "plan_revision": updated.plan_revision,
                        "from_state": current.state.value,
                        "to_state": updated.state.value,
                    },
                )
            self._mirror_after_commit_unlocked(run_id)
            return updated

    def replace_plan(self, run_id: str, replacement: Plan) -> RunRecord:
        """Replace a plan only after REPLAN_REQUIRED; new validation is needed."""
        if not isinstance(replacement, Plan):
            raise TypeError("replacement must be a Plan")
        with self._lock:
            with self._transaction():
                current = self._run_unlocked(run_id)
                updated = transition(
                    current,
                    RunState.PLANNED,
                    plan=self._plan_unlocked(current),
                    replacement_plan=replacement,
                )
                self._save_plan_unlocked(replacement)
                self._write_run_unlocked(updated)
                self._insert_event_unlocked(
                    run_id,
                    EventType.PLAN_REPLACED,
                    {
                        "plan_fingerprint": updated.plan_fingerprint,
                        "plan_revision": updated.plan_revision,
                        "from_state": current.state.value,
                        "to_state": updated.state.value,
                    },
                )
            self._mirror_after_commit_unlocked(run_id)
            return updated

    def record_approval(self, approval: ApprovalRecord) -> None:
        """Record an explicit decision at the matching gate and exact identity."""
        if not isinstance(approval, ApprovalRecord):
            raise TypeError("approval must be an ApprovalRecord")
        with self._lock:
            with self._transaction():
                run = self._run_unlocked(approval.run_id)
                if approval.plan_id != run.plan_id or approval.plan_revision != run.plan_revision:
                    raise ApprovalError("approval does not match the current plan ID and revision")
                if approval.approval_type == ApprovalType.PLAN_EXECUTION:
                    expected_state = RunState.WAITING_FOR_APPROVAL
                    expected_fingerprint = run.plan_fingerprint
                else:
                    expected_state = RunState.WAITING_FOR_APPLY_APPROVAL
                    expected_fingerprint = run.reviewed_result_fingerprint
                if run.state != expected_state:
                    raise ApprovalError(
                        f"{approval.approval_type} decision is only allowed in {expected_state}"
                    )
                if (
                    expected_fingerprint is None
                    or approval.subject_fingerprint != expected_fingerprint
                ):
                    raise ApprovalError("approval fingerprint does not match the current subject")
                self._connection.execute(
                    "INSERT INTO approvals (approval_id, run_id, approval_type, plan_id, plan_revision, "
                    "subject_fingerprint, timestamp, actor, decision, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        approval.approval_id,
                        approval.run_id,
                        approval.approval_type.value,
                        approval.plan_id,
                        approval.plan_revision,
                        approval.subject_fingerprint,
                        approval.timestamp.isoformat(),
                        approval.actor,
                        approval.decision.value,
                        approval.reason,
                    ),
                )
                self._insert_event_unlocked(
                    run.run_id,
                    EventType.APPROVAL_RECORDED,
                    {
                        "approval_type": approval.approval_type.value,
                        "decision": approval.decision.value,
                        "subject_fingerprint": approval.subject_fingerprint,
                        "plan_revision": approval.plan_revision,
                    },
                )
            self._mirror_after_commit_unlocked(approval.run_id)

    def set_reviewed_result(self, run_id: str, fingerprint: str) -> RunRecord:
        """Record a reviewed-result identity; this does not perform a review."""
        if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise StoreError("reviewed result fingerprint must be a SHA-256 digest")
        with self._lock:
            with self._transaction():
                run = self._run_unlocked(run_id)
                if run.state != RunState.REVIEWING:
                    raise StoreError("reviewed result identity can only be set in REVIEWING")
                updated = run.model_copy(
                    update={
                        "reviewed_result_fingerprint": fingerprint,
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._write_run_unlocked(updated)
                self._insert_event_unlocked(
                    run_id, EventType.RESULT_IDENTIFIED, {"result_fingerprint": fingerprint}
                )
            self._mirror_after_commit_unlocked(run_id)
            return updated
