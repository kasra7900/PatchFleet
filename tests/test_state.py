"""Pure state transitions and explicit approval guards."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from patchfleet.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalType,
    Plan,
    RunRecord,
    RunState,
    plan_fingerprint,
)
from patchfleet.state import TransitionError, transition

RESULT_FINGERPRINT = "a" * 64


def draft(plan: Plan) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        run_id="run-1",
        plan_id=plan.plan_id,
        plan_fingerprint=plan_fingerprint(plan),
        plan_revision=1,
        state=RunState.DRAFT,
        created_at=now,
        updated_at=now,
    )


def approval(
    run: RunRecord,
    approval_type: ApprovalType,
    subject: str,
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    approval_id: str = "approval-1",
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
    )


def waiting_for_plan_approval(plan: Plan) -> RunRecord:
    run = draft(plan)
    for target in (RunState.PLANNED, RunState.VALIDATED, RunState.WAITING_FOR_APPROVAL):
        run = transition(run, target, plan=plan)
    return run


def test_legal_success_path_requires_both_approvals(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan)
    execution = approval(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint)
    for target in (RunState.PROVISIONING, RunState.RUNNING, RunState.VERIFYING, RunState.REVIEWING):
        run = transition(run, target, plan=plan, approvals=[execution])
    run = run.model_copy(update={"reviewed_result_fingerprint": RESULT_FINGERPRINT})
    run = transition(run, RunState.WAITING_FOR_APPLY_APPROVAL, plan=plan, approvals=[execution])
    application = approval(
        run, ApprovalType.RESULT_APPLICATION, RESULT_FINGERPRINT, approval_id="approval-2"
    )
    run = transition(run, RunState.APPLIED, plan=plan, approvals=[execution, application])
    assert run.state == RunState.APPLIED


def test_illegal_transition_is_rejected(plan: Plan) -> None:
    with pytest.raises(TransitionError, match="cannot transition"):
        transition(draft(plan), RunState.RUNNING, plan=plan)


def test_state_is_frozen_and_target_must_be_enum(plan: Plan) -> None:
    run = draft(plan)
    with pytest.raises(ValidationError):
        run.state = "APPLIED"
    with pytest.raises(TypeError, match="RunState"):
        transition(run, "PLANNED", plan=plan)  # type: ignore[arg-type]


def test_provisioning_requires_exact_plan_approval(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan)
    with pytest.raises(TransitionError) as raised:
        transition(run, RunState.PROVISIONING, plan=plan)
    assert raised.value.code == "plan_approval_required"


def test_running_guard_rechecks_plan_approval(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan).model_copy(update={"state": RunState.PROVISIONING})
    with pytest.raises(TransitionError) as raised:
        transition(run, RunState.RUNNING, plan=plan)
    assert raised.value.code == "plan_approval_required"


def test_latest_rejection_blocks_execution(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan)
    decisions = [
        approval(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint),
        approval(
            run,
            ApprovalType.PLAN_EXECUTION,
            run.plan_fingerprint,
            decision=ApprovalDecision.REJECTED,
            approval_id="approval-2",
        ),
    ]
    with pytest.raises(TransitionError) as raised:
        transition(run, RunState.PROVISIONING, plan=plan, approvals=decisions)
    assert raised.value.code == "plan_approval_required"


def test_replanning_invalidates_prior_approval_even_for_same_plan(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan)
    old_approval = approval(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint)
    run = transition(run, RunState.REPLAN_REQUIRED, plan=plan, approvals=[old_approval])
    assert run.plan_revision == 2
    for target in (RunState.PLANNED, RunState.VALIDATED, RunState.WAITING_FOR_APPROVAL):
        run = transition(run, target, plan=plan, approvals=[old_approval])
    with pytest.raises(TransitionError) as raised:
        transition(run, RunState.PROVISIONING, plan=plan, approvals=[old_approval])
    assert raised.value.code == "plan_approval_required"


def test_waiting_for_apply_requires_result_identity(plan: Plan) -> None:
    run = waiting_for_plan_approval(plan)
    execution = approval(run, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint)
    for target in (RunState.PROVISIONING, RunState.RUNNING, RunState.VERIFYING, RunState.REVIEWING):
        run = transition(run, target, plan=plan, approvals=[execution])
    with pytest.raises(TransitionError) as raised:
        transition(run, RunState.WAITING_FOR_APPLY_APPROVAL, plan=plan, approvals=[execution])
    assert raised.value.code == "result_identity_required"


def test_failed_blocked_and_cancelled_cannot_apply(plan: Plan) -> None:
    for state in (RunState.FAILED, RunState.BLOCKED, RunState.CANCELLED):
        run = draft(plan).model_copy(
            update={"state": state, "reviewed_result_fingerprint": RESULT_FINGERPRINT}
        )
        application = approval(run, ApprovalType.RESULT_APPLICATION, RESULT_FINGERPRINT)
        with pytest.raises(TransitionError) as raised:
            transition(run, RunState.APPLIED, plan=plan, approvals=[application])
        assert raised.value.code == "illegal_transition"
