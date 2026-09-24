"""The one guarded, in-memory transition function for run state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from .contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalType,
    Plan,
    RunRecord,
    RunState,
    plan_fingerprint,
)
from .validation import PlanValidationError, validate_plan

LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.DRAFT: frozenset({RunState.PLANNED, RunState.CANCELLED}),
    RunState.PLANNED: frozenset({RunState.VALIDATED, RunState.REPLAN_REQUIRED, RunState.CANCELLED}),
    RunState.VALIDATED: frozenset(
        {RunState.WAITING_FOR_APPROVAL, RunState.REPLAN_REQUIRED, RunState.CANCELLED}
    ),
    RunState.WAITING_FOR_APPROVAL: frozenset(
        {RunState.PROVISIONING, RunState.REPLAN_REQUIRED, RunState.CANCELLED}
    ),
    RunState.PROVISIONING: frozenset(
        {
            RunState.RUNNING,
            RunState.BLOCKED,
            RunState.FAILED,
            RunState.REPLAN_REQUIRED,
            RunState.CANCELLED,
        }
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.VERIFYING,
            RunState.BLOCKED,
            RunState.FAILED,
            RunState.REPLAN_REQUIRED,
            RunState.CANCELLED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {RunState.REVIEWING, RunState.FAILED, RunState.REPLAN_REQUIRED, RunState.CANCELLED}
    ),
    RunState.REVIEWING: frozenset(
        {
            RunState.WAITING_FOR_APPLY_APPROVAL,
            RunState.FAILED,
            RunState.REPLAN_REQUIRED,
            RunState.CANCELLED,
        }
    ),
    RunState.WAITING_FOR_APPLY_APPROVAL: frozenset(
        {RunState.APPLIED, RunState.REPLAN_REQUIRED, RunState.CANCELLED}
    ),
    RunState.BLOCKED: frozenset({RunState.REPLAN_REQUIRED, RunState.CANCELLED}),
    RunState.FAILED: frozenset({RunState.REPLAN_REQUIRED, RunState.CANCELLED}),
    RunState.REPLAN_REQUIRED: frozenset({RunState.PLANNED, RunState.CANCELLED}),
    RunState.CANCELLED: frozenset(),
    RunState.APPLIED: frozenset(),
}


class TransitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _latest_decision(
    run: RunRecord,
    approvals: Sequence[ApprovalRecord],
    approval_type: ApprovalType,
    subject_fingerprint: str,
) -> ApprovalDecision | None:
    matching = (
        approval
        for approval in approvals
        if approval.run_id == run.run_id
        and approval.plan_id == run.plan_id
        and approval.plan_revision == run.plan_revision
        and approval.approval_type == approval_type
        and approval.subject_fingerprint == subject_fingerprint
    )
    return next((approval.decision for approval in reversed(tuple(matching))), None)


def has_plan_execution_approval(run: RunRecord, approvals: Sequence[ApprovalRecord]) -> bool:
    """Check the latest decision for the exact run, revision, and plan fingerprint."""
    return (
        _latest_decision(run, approvals, ApprovalType.PLAN_EXECUTION, run.plan_fingerprint)
        == ApprovalDecision.APPROVED
    )


def transition(
    run: RunRecord,
    target: RunState,
    *,
    plan: Plan,
    approvals: Sequence[ApprovalRecord] = (),
    replacement_plan: Plan | None = None,
) -> RunRecord:
    """Return a new run only when the transition and approvals are valid.

    Approvals are supplied in recorded order, oldest first. A later rejection
    supersedes an earlier approval for the same plan revision or result.
    """
    if not isinstance(target, RunState):
        raise TypeError("target must be a RunState")
    if target not in LEGAL_TRANSITIONS[run.state]:
        raise TransitionError(
            "illegal_transition", f"cannot transition from {run.state} to {target}"
        )
    if plan.plan_id != run.plan_id or plan_fingerprint(plan) != run.plan_fingerprint:
        raise TransitionError("plan_identity_mismatch", "run is not pinned to this exact plan")
    if replacement_plan is not None and (
        run.state != RunState.REPLAN_REQUIRED or target != RunState.PLANNED
    ):
        raise TransitionError(
            "replacement_not_allowed", "plan replacement requires REPLAN_REQUIRED -> PLANNED"
        )
    if replacement_plan is not None and replacement_plan.plan_id != run.plan_id:
        raise TransitionError("plan_id_mismatch", "replacement plan must keep the run's plan ID")

    if target == RunState.VALIDATED:
        try:
            validate_plan(plan)
        except PlanValidationError as error:
            raise TransitionError("plan_invalid", str(error)) from error

    if target in {RunState.PROVISIONING, RunState.RUNNING} and not has_plan_execution_approval(
        run, approvals
    ):
        raise TransitionError(
            "plan_approval_required", "exact current plan requires explicit execution approval"
        )

    if target == RunState.WAITING_FOR_APPLY_APPROVAL and run.reviewed_result_fingerprint is None:
        raise TransitionError(
            "result_identity_required", "reviewed result fingerprint must be recorded first"
        )

    if target == RunState.APPLIED:
        result = run.reviewed_result_fingerprint
        if (
            result is None
            or _latest_decision(run, approvals, ApprovalType.RESULT_APPLICATION, result)
            != ApprovalDecision.APPROVED
        ):
            raise TransitionError(
                "result_approval_required",
                "exact reviewed result requires explicit application approval",
            )

    updates: dict[str, object] = {"state": target, "updated_at": datetime.now(UTC)}
    if target == RunState.REPLAN_REQUIRED:
        # An approval from before replanning cannot authorize another attempt,
        # even if the proposed content is later changed back byte-for-byte.
        updates["plan_revision"] = run.plan_revision + 1
        updates["reviewed_result_fingerprint"] = None
    if replacement_plan is not None:
        updates["plan_fingerprint"] = plan_fingerprint(replacement_plan)
    return run.model_copy(update=updates)
