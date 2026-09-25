"""In-memory, UI-facing view models for a live fleet.

Raw Worker output may exist only here, in a bounded in-memory buffer. It is never
written to events, SQLite, JSONL, preferences, or telemetry. The monitor is
advisory: the durable store and existing guards remain the source of truth.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Any

from .contracts import Plan, plan_fingerprint

MAX_OUTPUT_LINES = 200
MAX_OUTPUT_LINE = 2000
MAX_EVENTS = 100

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str) -> tuple[str, bool]:
    """Strip terminal control sequences and bound the length; report truncation."""
    cleaned = _CONTROL.sub("", _ANSI.sub("", text))
    if len(cleaned) > MAX_OUTPUT_LINE:
        return cleaned[:MAX_OUTPUT_LINE], True
    return cleaned, False


@dataclass(frozen=True)
class WorkerPanel:
    task_id: str
    title: str
    provider: str
    model: str
    state: str
    attempt: int
    worktree: str | None
    reason: str | None
    dependencies: tuple[str, ...]
    elapsed_seconds: float
    output: tuple[str, ...]
    output_truncated: bool


@dataclass(frozen=True)
class LeaderPanel:
    leader: str
    run_id: str | None
    plan_id: str
    plan_fingerprint: str
    run_state: str
    approved: bool
    task_counts: dict[str, int]
    recent_events: tuple[str, ...]
    next_action: str
    phase_note: str


@dataclass(frozen=True)
class FleetSnapshot:
    run_id: str | None
    run_state: str
    leader: LeaderPanel
    workers: tuple[WorkerPanel, ...]


@dataclass(frozen=True)
class FleetLayout:
    columns: int
    rows: int
    paged: bool
    compact: bool

    def capacity(self) -> int:
        return max(self.columns * self.rows, 1)


def worker_layout(count: int, *, width: int, height: int) -> FleetLayout:
    """Choose a readable panel arrangement for the current terminal size."""
    if count <= 0:
        return FleetLayout(0, 0, False, False)
    if width < 80 or height < 18:
        return FleetLayout(1, 1, count > 1, True)
    if count == 1:
        return FleetLayout(1, 1, False, False)
    if count <= 4:
        return FleetLayout(2, 2 if count > 2 else 1, False, False)
    return FleetLayout(2, 2, True, False)


def page_count(count: int, layout: FleetLayout) -> int:
    if count <= 0:
        return 1
    return max(1, (count + layout.capacity() - 1) // layout.capacity())


class _WorkerState:
    __slots__ = (
        "task_id",
        "title",
        "provider",
        "model",
        "state",
        "attempt",
        "worktree",
        "reason",
        "dependencies",
        "output",
        "output_truncated",
        "started_at",
    )

    def __init__(
        self, task_id: str, title: str, provider: str, model: str, dependencies: tuple[str, ...]
    ):
        self.task_id = task_id
        self.title = title
        self.provider = provider
        self.model = model
        self.state = "PENDING"
        self.attempt = 0
        self.worktree: str | None = None
        self.reason: str | None = None
        self.dependencies = dependencies
        self.output: deque[str] = deque(maxlen=MAX_OUTPUT_LINES)
        self.output_truncated = False
        self.started_at: float | None = None


class FleetMonitor:
    """Thread-safe, bounded live view of one run for the interactive UI."""

    def __init__(self, repository: str, plan: Plan) -> None:
        self.repository = repository
        self.plan = plan
        self.plan_fingerprint = plan_fingerprint(plan)
        self.run_id: str | None = None
        self._lock = threading.Lock()
        self._run_state = "DRAFT"
        self._approved = False
        self._events: deque[str] = deque(maxlen=MAX_EVENTS)
        self._workers: dict[str, _WorkerState] = {
            task.task_id: _WorkerState(
                task.task_id,
                task.title,
                task.assigned_provider,
                task.selected_model,
                task.dependencies,
            )
            for task in plan.tasks
        }
        self.completed = threading.Event()

    def attach_run(self, run_id: str) -> None:
        with self._lock:
            self.run_id = run_id

    def mark_approved(self) -> None:
        with self._lock:
            self._approved = True

    def add_event(self, text: str) -> None:
        cleaned, _ = sanitize_text(text)
        if not cleaned.strip():
            return
        with self._lock:
            self._events.append(cleaned)

    def run_state(self, state: str) -> None:
        with self._lock:
            self._run_state = state
            self._events.append(f"run state: {state}")
        if state in {"VERIFYING", "FAILED", "BLOCKED", "CANCELLED", "APPLIED"}:
            self.completed.set()

    def task_state(self, task_id: str, state: str, attempt: int, reason: str | None = None) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
            if worker is None:
                return
            worker.state = state
            if attempt:
                worker.attempt = attempt
            worker.reason = reason
            if state == "RUNNING" and worker.started_at is None:
                worker.started_at = monotonic()
            self._events.append(f"{task_id}: {state}" + (f" ({reason})" if reason else ""))

    def set_worktree(self, task_id: str, worktree: str) -> None:
        with self._lock:
            worker = self._workers.get(task_id)
            if worker is not None:
                worker.worktree = worktree

    def worktree(self, task_id: str, path: str) -> None:
        """Observer-compatible alias for :meth:`set_worktree`."""
        self.set_worktree(task_id, path)

    def task_output(self, task_id: str, stream: str, line: str) -> None:
        cleaned, truncated = sanitize_text(line)
        if not cleaned and not line:
            return
        with self._lock:
            worker = self._workers.get(task_id)
            if worker is None:
                return
            worker.output.append(cleaned)
            if truncated:
                worker.output_truncated = True

    def snapshot(self) -> FleetSnapshot:
        now = monotonic()
        with self._lock:
            workers = tuple(
                WorkerPanel(
                    task_id=worker.task_id,
                    title=worker.title,
                    provider=worker.provider,
                    model=worker.model,
                    state=worker.state,
                    attempt=worker.attempt,
                    worktree=worker.worktree,
                    reason=worker.reason,
                    dependencies=worker.dependencies,
                    elapsed_seconds=(now - worker.started_at) if worker.started_at else 0.0,
                    output=tuple(worker.output),
                    output_truncated=worker.output_truncated,
                )
                for worker in (self._workers[key] for key in sorted(self._workers))
            )
            counts: dict[str, int] = {}
            for worker in workers:
                counts[worker.state] = counts.get(worker.state, 0) + 1
            leader_label = (
                f"{self.plan.leader.assigned_provider} / {self.plan.leader.selected_model}"
            )
            leader = LeaderPanel(
                leader=leader_label,
                run_id=self.run_id,
                plan_id=self.plan.plan_id,
                plan_fingerprint=self.plan_fingerprint,
                run_state=self._run_state,
                approved=self._approved,
                task_counts=counts,
                recent_events=tuple(self._events),
                next_action=_next_action(self._run_state, counts),
                phase_note=(
                    "Phase 2 ends at VERIFYING. Verification, review, and Git application "
                    "are later phases."
                ),
            )
            return FleetSnapshot(
                run_id=self.run_id, run_state=self._run_state, leader=leader, workers=workers
            )


def _next_action(state: str, counts: dict[str, int]) -> str:
    if state in {"DRAFT", "PLANNED", "VALIDATED"}:
        return "Review the plan, then approve to create the durable run."
    if state == "WAITING_FOR_APPROVAL":
        return "Approve the exact plan fingerprint, then start the fleet."
    if state == "PROVISIONING":
        return "Provisioning worktrees."
    if state == "RUNNING":
        running = counts.get("RUNNING", 0)
        pending = counts.get("PENDING", 0) + counts.get("PROVISIONED", 0)
        return f"{running} running, {pending} queued. Phase 2 stops at VERIFYING."
    if state == "VERIFYING":
        return "All tasks succeeded. Phase 2 hands off at VERIFYING."
    if state == "FAILED":
        return "Inspect failed Worker panels; a new run requires a new approved plan."
    if state == "BLOCKED":
        return "Resolve the blocked reason shown on the affected panels."
    if state == "CANCELLED":
        return "Run cancelled. Start a new request to plan again."
    return "Inspect the plan and fleet state."


def summarize_worker(worker: WorkerPanel) -> str:
    """Compact one-line label used by tests and small layouts."""
    return f"{worker.task_id} {worker.provider}/{worker.model} {worker.state}"


def snapshot_dict(snapshot: FleetSnapshot) -> dict[str, Any]:
    """Stable serializable shape for tests without coupling to widget internals."""
    return {
        "run_state": snapshot.run_state,
        "leader": snapshot.leader.leader,
        "workers": [
            {
                "task_id": worker.task_id,
                "state": worker.state,
                "provider": worker.provider,
                "model": worker.model,
                "output_lines": len(worker.output),
                "output": list(worker.output),
                "truncated": worker.output_truncated,
            }
            for worker in snapshot.workers
        ],
    }
