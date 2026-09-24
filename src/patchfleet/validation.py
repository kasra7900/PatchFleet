"""Deterministic plan validation, independent of agent providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import ValidationError

from .contracts import Plan, Role


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str


class PlanValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


def _location(parts: tuple[str | int, ...]) -> str:
    location = ""
    for part in parts:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += ("." if location else "") + str(part)
    return location or "plan"


def _path_issue(path: str) -> str | None:
    if not path or path == ".":
        return "path must name a repository-relative file or directory"
    if "\\" in path or any(ord(character) < 32 for character in path):
        return "path must use safe forward-slash components"
    if PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive:
        return "absolute paths are not allowed"
    if any(component in ("", ".", "..") for component in path.split("/")):
        return "path must not contain empty, current-directory, or parent-directory components"
    return None


def _dependency_cycle(dependencies: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()

    def visit(task_id: str) -> tuple[str, ...] | None:
        visited.add(task_id)
        active.append(task_id)
        active_set.add(task_id)
        for dependency in sorted(dependencies[task_id]):
            if dependency not in dependencies:
                continue
            if dependency in active_set:
                start = active.index(dependency)
                return tuple(active[start:] + [dependency])
            if dependency not in visited:
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle
        active.pop()
        active_set.remove(task_id)
        return None

    for task_id in sorted(dependencies):
        if task_id not in visited:
            cycle = visit(task_id)
            if cycle is not None:
                return cycle
    return None


def validate_plan(value: Any) -> Plan:
    """Parse and validate a plan, raising structured issues on failure."""
    try:
        plan = value if isinstance(value, Plan) else Plan.model_validate(value)
    except ValidationError as error:
        issues = [
            ValidationIssue(
                path=_location(tuple(item["loc"])),
                code=item["type"],
                message=item["msg"],
            )
            for item in error.errors()
        ]
        raise PlanValidationError(issues) from error

    issues: list[ValidationIssue] = []
    if plan.leader.selected_role != Role.LEADER:
        issues.append(
            ValidationIssue(
                "leader.selected_role", "role_mismatch", "leader assignment must have role 'leader'"
            )
        )
    if plan.reviewer.selected_role != Role.REVIEWER:
        issues.append(
            ValidationIssue(
                "reviewer.selected_role", "role_mismatch", "plan reviewer must have role 'reviewer'"
            )
        )

    first_index: dict[str, int] = {}
    for index, task in enumerate(plan.tasks):
        if task.task_id in first_index:
            issues.append(
                ValidationIssue(
                    f"tasks[{index}].task_id",
                    "duplicate_task_id",
                    f"task ID '{task.task_id}' duplicates tasks[{first_index[task.task_id]}]",
                )
            )
        else:
            first_index[task.task_id] = index
        if task.selected_role != Role.WORKER:
            issues.append(
                ValidationIssue(
                    f"tasks[{index}].selected_role",
                    "role_mismatch",
                    "task assignment must have role 'worker'",
                )
            )
        if task.reviewer.selected_role != Role.REVIEWER:
            issues.append(
                ValidationIssue(
                    f"tasks[{index}].reviewer.selected_role",
                    "role_mismatch",
                    "task reviewer must have role 'reviewer'",
                )
            )
        for path_index, path in enumerate(task.allowed_paths):
            problem = _path_issue(path)
            if problem is not None:
                issues.append(
                    ValidationIssue(
                        f"tasks[{index}].allowed_paths[{path_index}]", "invalid_path", problem
                    )
                )

    dependencies: dict[str, tuple[str, ...]] = {}
    for index, task in enumerate(plan.tasks):
        dependencies.setdefault(task.task_id, task.dependencies)
        for dependency in task.dependencies:
            if dependency == task.task_id:
                issues.append(
                    ValidationIssue(
                        f"tasks[{index}].dependencies",
                        "self_dependency",
                        "task cannot depend on itself",
                    )
                )
            elif dependency not in first_index:
                issues.append(
                    ValidationIssue(
                        f"tasks[{index}].dependencies",
                        "missing_dependency",
                        f"unknown task ID '{dependency}'",
                    )
                )

    if not any(issue.code == "duplicate_task_id" for issue in issues):
        cycle = _dependency_cycle(dependencies)
        if cycle is not None:
            issues.append(
                ValidationIssue(
                    "tasks", "dependency_cycle", "dependency cycle: " + " -> ".join(cycle)
                )
            )

    if issues:
        raise PlanValidationError(issues)
    return plan
