"""Explicit handoff from a validated Leader draft to the durable Phase 1/2 run.

This module reuses the existing run/approval/execution foundation exactly. It
never weakens the fingerprint, revision, or approval guards, and it never starts
a Worker merely because a plan exists.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .contracts import Plan, plan_fingerprint
from .execution import ExecutionObserver, approve_run, create_run, start_run
from .validation import PlanValidationError, validate_plan


class HandoffError(ValueError):
    """An approved-plan handoff cannot safely proceed."""


@dataclass(frozen=True)
class ApprovedPlan:
    run_id: str
    plan: Plan
    plan_fingerprint: str
    repository: Path
    actor: str


def approve_plan(plan: Plan, repository: Path, actor: str) -> ApprovedPlan:
    """Create the durable run and record the existing PLAN_EXECUTION approval."""
    if not isinstance(plan, Plan):
        raise HandoffError("a validated PatchFleet Plan is required")
    actor = actor.strip()
    if not actor:
        raise HandoffError("an explicit local actor is required for approval")
    try:
        validated = validate_plan(plan)
    except PlanValidationError as error:
        raise HandoffError(f"the plan is not valid: {error}") from error
    root = Path(repository)
    run_id = create_run(validated, root)
    approve_run(run_id, actor, root)
    return ApprovedPlan(
        run_id=run_id,
        plan=validated,
        plan_fingerprint=plan_fingerprint(validated),
        repository=root,
        actor=actor,
    )


async def start_fleet_async(
    handoff: ApprovedPlan,
    observer: ExecutionObserver | None = None,
    *,
    cancel_event: asyncio.Event | None = None,
    task_cancel_events: Mapping[str, threading.Event] | None = None,
) -> str:
    """Provision worktrees and start Workers; only valid after explicit approval."""
    state = await start_run(
        handoff.run_id,
        handoff.repository,
        observer,
        cancel_event=cancel_event,
        task_cancel_events=task_cancel_events,
    )
    return state.value


def start_fleet(
    handoff: ApprovedPlan,
    observer: ExecutionObserver | None = None,
    *,
    cancel_event: asyncio.Event | None = None,
    task_cancel_events: Mapping[str, threading.Event] | None = None,
) -> str:
    return asyncio.run(
        start_fleet_async(
            handoff,
            observer,
            cancel_event=cancel_event,
            task_cancel_events=task_cancel_events,
        )
    )
