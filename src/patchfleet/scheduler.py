"""Pure deterministic dependency selection; no provider work occurs here."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .contracts import Plan


class TaskState(StrEnum):
    PENDING = "PENDING"
    PROVISIONED = "PROVISIONED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


def ready_tasks(plan: Plan, states: Mapping[str, TaskState], slots: int) -> tuple[str, ...]:
    """Choose lexical ready tasks, treating unknown dependencies as unsatisfied."""
    if slots < 0:
        raise ValueError("slots cannot be negative")
    return tuple(
        task.task_id
        for task in sorted(plan.tasks, key=lambda item: item.task_id)
        if states.get(task.task_id) == TaskState.PENDING
        and all(states.get(dependency) == TaskState.SUCCEEDED for dependency in task.dependencies)
    )[:slots]


def blocked_dependents(plan: Plan, states: Mapping[str, TaskState]) -> tuple[str, ...]:
    """Return pending tasks with a terminal unsuccessful prerequisite."""
    bad = {TaskState.FAILED, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.INTERRUPTED}
    return tuple(
        task.task_id
        for task in sorted(plan.tasks, key=lambda item: item.task_id)
        if states.get(task.task_id) == TaskState.PENDING
        and any(states.get(dependency) in bad for dependency in task.dependencies)
    )
