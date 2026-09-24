"""SQLite durability, approval identity, and JSONL recovery."""

import json
from datetime import UTC, datetime

import pytest

from patchfleet.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalType,
    Plan,
    RunRecord,
    RunState,
)
from patchfleet.state import TransitionError
from patchfleet.storage import ApprovalError, EventLogCorruption, SQLiteStore

RESULT_FINGERPRINT = "b" * 64


def approve(
    run: RunRecord,
    approval_type: ApprovalType,
    subject: str,
    *,
    approval_id: str = "approval-1",
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    reason: str | None = None,
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        run_id=run.run_id,
        approval_type=approval_type,
        plan_id=run.plan_id,
        plan_revision=run.plan_revision,
        subject_fingerprint=subject,
        timestamp=datetime.now(UTC),
        actor="maintainer",
        decision=decision,
        reason=reason,
    )


def awaiting_plan(store: SQLiteStore, plan: Plan) -> RunRecord:
    store.create_run("run-1", plan)
    for target in (RunState.PLANNED, RunState.VALIDATED, RunState.WAITING_FOR_APPROVAL):
        run = store.transition_run("run-1", target)
    return run


def awaiting_result(store: SQLiteStore, plan: Plan) -> RunRecord:
    run = awaiting_plan(store, plan)
    store.record_approval(approve(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint))
    for target in (RunState.PROVISIONING, RunState.RUNNING, RunState.VERIFYING, RunState.REVIEWING):
        run = store.transition_run("run-1", target)
    store.set_reviewed_result("run-1", RESULT_FINGERPRINT)
    return store.transition_run("run-1", RunState.WAITING_FOR_APPLY_APPROVAL)


def test_sqlite_round_trip_and_ordered_safe_events(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        run = awaiting_plan(store, plan)
        store.record_approval(
            approve(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint, reason="private note")
        )
        assert [event.sequence for event in store.get_events("run-1")] == [1, 2, 3, 4, 5]

    with SQLiteStore(tmp_path) as reopened:
        assert reopened.get_run("run-1").state == RunState.WAITING_FOR_APPROVAL
        assert reopened.get_plan("run-1") == plan
        assert reopened.get_approvals("run-1")[0].decision == ApprovalDecision.APPROVED
        events = reopened.get_events("run-1")
        assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
        reopened.transition_run("run-1", RunState.PROVISIONING)
        assert reopened.get_events("run-1")[-1].sequence == 6

    log = (tmp_path / "events" / "run-1.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(line) for line in log.splitlines()]
    assert [line["sequence"] for line in lines] == [1, 2, 3, 4, 5, 6]
    assert "private note" not in log
    assert plan.purpose not in log
    assert all("payload" in line and "timestamp" in line and "run_id" in line for line in lines)


def test_invalid_plan_cannot_enter_validated(tmp_path, plan_data: dict) -> None:
    plan_data["leader"]["selected_role"] = "worker"
    unvalidated = Plan.model_validate(plan_data)
    with SQLiteStore(tmp_path) as store:
        store.create_run("run-1", unvalidated)
        store.transition_run("run-1", RunState.PLANNED)
        with pytest.raises(TransitionError) as raised:
            store.transition_run("run-1", RunState.VALIDATED)
        assert raised.value.code == "plan_invalid"
        assert store.get_run("run-1").state == RunState.PLANNED
        assert len(store.get_events("run-1")) == 2


def test_plan_revision_requires_new_validation_and_approval(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        run = awaiting_plan(store, plan)
        old_approval = approve(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint)
        store.record_approval(old_approval)
        store.transition_run("run-1", RunState.PROVISIONING)
        store.transition_run("run-1", RunState.REPLAN_REQUIRED)
        changed = plan.model_copy(update={"purpose": "A revised purpose."})
        run = store.replace_plan("run-1", changed)
        assert run.state == RunState.PLANNED
        assert run.plan_revision == 2
        assert run.plan_fingerprint != old_approval.subject_fingerprint
        store.transition_run("run-1", RunState.VALIDATED)
        run = store.transition_run("run-1", RunState.WAITING_FOR_APPROVAL)
        with pytest.raises(ApprovalError, match="revision"):
            store.record_approval(old_approval)
        with pytest.raises(TransitionError) as raised:
            store.transition_run("run-1", RunState.PROVISIONING)
        assert raised.value.code == "plan_approval_required"
        store.record_approval(
            approve(
                run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint, approval_id="approval-2"
            )
        )
        assert store.transition_run("run-1", RunState.PROVISIONING).state == RunState.PROVISIONING


def test_rejected_approval_blocks_execution(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        run = awaiting_plan(store, plan)
        store.record_approval(approve(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint))
        store.record_approval(
            approve(
                run,
                ApprovalType.PLAN_EXECUTION,
                run.plan_fingerprint,
                approval_id="approval-2",
                decision=ApprovalDecision.REJECTED,
            )
        )
        with pytest.raises(TransitionError) as raised:
            store.transition_run("run-1", RunState.PROVISIONING)
        assert raised.value.code == "plan_approval_required"


def test_application_requires_exact_result_approval(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        run = awaiting_result(store, plan)
        with pytest.raises(TransitionError) as raised:
            store.transition_run("run-1", RunState.APPLIED)
        assert raised.value.code == "result_approval_required"
        with pytest.raises(ApprovalError, match="fingerprint"):
            store.record_approval(
                approve(run, ApprovalType.RESULT_APPLICATION, "c" * 64, approval_id="approval-2")
            )
        store.record_approval(
            approve(
                run,
                ApprovalType.RESULT_APPLICATION,
                RESULT_FINGERPRINT,
                approval_id="approval-2",
                decision=ApprovalDecision.REJECTED,
            )
        )
        with pytest.raises(TransitionError):
            store.transition_run("run-1", RunState.APPLIED)
        store.record_approval(
            approve(
                run, ApprovalType.RESULT_APPLICATION, RESULT_FINGERPRINT, approval_id="approval-3"
            )
        )
        assert store.transition_run("run-1", RunState.APPLIED).state == RunState.APPLIED


def test_missing_jsonl_suffix_is_reconciled_from_sqlite(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        awaiting_plan(store, plan)
        expected = len(store.get_events("run-1"))
    log_path = tmp_path / "events" / "run-1.jsonl"
    first_line = log_path.read_bytes().splitlines(keepends=True)[0]
    log_path.write_bytes(first_line + b'{"incomplete":')

    with SQLiteStore(tmp_path) as reopened:
        assert len(reopened.get_events("run-1")) == expected
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == expected
    assert "incomplete" not in log_path.read_text(encoding="utf-8")


def test_mirror_write_failure_keeps_committed_state(tmp_path, plan: Plan, monkeypatch) -> None:
    with SQLiteStore(tmp_path) as store:

        def fail_mirror(_run_id: str) -> None:
            raise OSError("simulated interrupted mirror write")

        monkeypatch.setattr(store, "_mirror_run_unlocked", fail_mirror)
        store.create_run("run-1", plan)
        assert store.last_mirror_error is not None
        assert store.get_run("run-1").state == RunState.DRAFT

    with SQLiteStore(tmp_path) as reopened:
        assert reopened.get_events("run-1")[0].sequence == 1
    assert (tmp_path / "events" / "run-1.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_completed_jsonl_corruption_is_detected(tmp_path, plan: Plan) -> None:
    with SQLiteStore(tmp_path) as store:
        store.create_run("run-1", plan)
    log_path = tmp_path / "events" / "run-1.jsonl"
    with log_path.open("ab") as log:
        log.write(b'{"unexpected":true}\n')
    with pytest.raises(EventLogCorruption):
        SQLiteStore(tmp_path)
