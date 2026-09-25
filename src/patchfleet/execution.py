"""Approved local Worker dispatch without review, verification, or application."""

from __future__ import annotations

import asyncio
import fcntl
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .adapters import ADAPTERS
from .adapters.base import AdapterRequest, AdapterResult, AdapterUnavailable, CLIAdapter
from .config import ConfigError, LocalConfig, load_config, resolve_executable
from .contracts import ApprovalDecision, ApprovalRecord, ApprovalType, Plan, RunState, TaskSpec
from .processes import supervise
from .scheduler import TaskState, blocked_dependents, ready_tasks
from .state import has_plan_execution_approval
from .storage import SQLiteStore, StoreError
from .worktrees import (
    WorktreeError,
    WorktreeInfo,
    changed_paths,
    ensure_local_metadata,
    inspect_repository,
    outside_scope,
    primary_snapshot,
    provision,
)


class ExecutionError(ValueError):
    """The requested local execution cannot safely proceed."""


class ExecutionObserver(Protocol):
    """Optional live view hook for the interactive UI.

    Implementations must remain advisory: the observer never changes scheduling,
    guards, or persistence. Live output handed to an observer is display-only.
    """

    def task_output(self, task_id: str, stream: str, line: str) -> None: ...

    def task_state(
        self, task_id: str, state: str, attempt: int, reason: str | None = None
    ) -> None: ...

    def run_state(self, state: str) -> None: ...

    def worktree(self, task_id: str, path: str) -> None: ...


async def _forward_event(source: asyncio.Event, target: asyncio.Event) -> None:
    await source.wait()
    target.set()


async def _forward_thread(source: threading.Event, target: asyncio.Event) -> None:
    while not source.is_set():
        await asyncio.sleep(0.05)
    target.set()


@contextmanager
def execution_lock(repository: Path):
    """Exclude concurrent executors and make restart recovery unambiguous."""
    path = repository / ".patchfleet" / "execution.lock"
    path.parent.mkdir(exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExecutionError(
                "another PatchFleet executor is active for this repository"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def create_run(plan: Plan, repository: Path) -> str:
    root, base = inspect_repository(repository)
    ensure_local_metadata(root)
    run_id = f"run-{uuid4().hex}"
    with SQLiteStore(root / ".patchfleet") as store:
        store.create_run(run_id, plan)
        for state in (RunState.PLANNED, RunState.VALIDATED, RunState.WAITING_FOR_APPROVAL):
            store.transition_run(run_id, state)
        store.initialize_execution(run_id, root, base)
    return run_id


def approve_run(run_id: str, actor: str, repository: Path) -> None:
    root, _ = inspect_repository(repository)
    with SQLiteStore(root / ".patchfleet") as store:
        run = store.get_run(run_id)
        target = store.get_target(run_id)
        if target["target_repository"] != str(root):
            raise ExecutionError("run target repository does not match --repo")
        store.record_approval(
            ApprovalRecord(
                approval_id=f"approval-{uuid4().hex}",
                run_id=run_id,
                approval_type=ApprovalType.PLAN_EXECUTION,
                plan_id=run.plan_id,
                plan_revision=run.plan_revision,
                subject_fingerprint=run.plan_fingerprint,
                timestamp=datetime.now(UTC),
                actor=actor,
                decision=ApprovalDecision.APPROVED,
            )
        )


async def inspect_capabilities(
    repository: Path, config: LocalConfig | None = None
) -> list[dict[str, object]]:
    """Read-only local checks; never contact providers or select a model."""
    if config is None:
        config = load_config(repository)
    result: list[dict[str, object]] = []
    for provider_id, provider in sorted(config.providers.items()):
        row: dict[str, object] = {
            "provider": provider_id,
            "enabled": provider.enabled,
            "executable": provider.executable,
        }
        if not provider.enabled:
            row.update({"available": False, "reason": "disabled by configuration"})
        elif provider_id not in ADAPTERS:
            row.update({"available": False, "reason": "no Phase 2 adapter for this provider"})
        elif (resolved := resolve_executable(repository, provider.executable)) is None:
            row.update(
                {"available": False, "reason": "configured executable not found or not executable"}
            )
        else:
            capability = await ADAPTERS[provider_id](resolved).detect()
            row.update(
                {
                    "available": capability.available,
                    "version": capability.version,
                    "model_flag": capability.model_selection_supported,
                    "reason": capability.reason,
                }
            )
        result.append(row)
    return result


def _task_states(store: SQLiteStore, run_id: str) -> dict[str, TaskState]:
    return {
        str(row["task_id"]): TaskState(str(row["state"])) for row in store.list_task_runs(run_id)
    }


async def _execute_task(
    store: SQLiteStore,
    run_id: str,
    task: TaskSpec,
    info: WorktreeInfo,
    adapter: CLIAdapter,
    baseline: tuple[str, bytes],
    observer: ExecutionObserver | None = None,
    cancel_event: asyncio.Event | None = None,
    task_cancel_event: threading.Event | None = None,
) -> None:
    request = AdapterRequest(
        run_id,
        task.task_id,
        task.assigned_provider,
        task.selected_model,
        task.selected_role,
        info.worktree_path,
        task.title,
        task.purpose,
        task.allowed_paths,
        task.acceptance_criteria,
        task.budget_limits,
    )
    command, prompt = adapter.build_command(request)
    attempt_id = store.begin_attempt(run_id, task.task_id)
    if observer is not None:
        observer.task_state(task.task_id, TaskState.RUNNING.value, 1)
    local_cancel = asyncio.Event()
    watchers: list[asyncio.Task] = []
    if cancel_event is not None:
        watchers.append(asyncio.create_task(_forward_event(cancel_event, local_cancel)))
    if task_cancel_event is not None:
        watchers.append(asyncio.create_task(_forward_thread(task_cancel_event, local_cancel)))
    try:
        process = await supervise(
            command,
            cwd=info.worktree_path,
            stdin=prompt,
            timeout=task.budget_limits.max_wall_time_seconds,
            max_output_bytes=task.budget_limits.max_output_bytes,
            on_start=lambda pid: store.set_attempt_pid(attempt_id, pid),
            on_output=(
                (lambda stream, line: observer.task_output(task.task_id, stream, line))
                if observer is not None
                else None
            ),
            cancel_event=local_cancel if watchers else None,
        )
        result = AdapterResult.from_process(process)
        paths = changed_paths(info)
        violations = outside_scope(paths, task.allowed_paths)
        reason = (
            "primary checkout changed during Worker execution"
            if primary_snapshot(info.target_repository) != baseline
            else "out-of-scope changed paths"
            if violations
            else "worker timed out"
            if result.timed_out
            else "worker cancelled"
            if result.cancelled
            else f"worker exited with code {result.exit_code}"
            if result.exit_code != 0
            else None
        )
    except (OSError, WorktreeError, AdapterUnavailable) as error:
        result = AdapterResult("failed", None, 0.0, False, False, 0, 0, False, 0)
        paths, violations, reason = (), (), str(error)
    finally:
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
    store.finish_attempt(run_id, task.task_id, attempt_id, result, paths, violations, reason)
    if observer is not None:
        final_state = (
            TaskState.CANCELLED
            if result.cancelled
            else TaskState.SUCCEEDED
            if result.status == "succeeded" and not violations and not reason
            else TaskState.FAILED
        )
        observer.task_state(task.task_id, final_state.value, 1, reason)


async def start_run(
    run_id: str,
    repository: Path,
    observer: ExecutionObserver | None = None,
    *,
    cancel_event: asyncio.Event | None = None,
    task_cancel_events: Mapping[str, threading.Event] | None = None,
) -> RunState:
    root, current_base = inspect_repository(repository)
    with execution_lock(root), SQLiteStore(root / ".patchfleet") as store:
        if store.recover_interrupted(run_id):
            raise ExecutionError(
                "previous Worker attempt was interrupted; explicit replanning is required"
            )
        run = store.get_run(run_id)
        target = store.get_target(run_id)
        if target["target_repository"] != str(root) or target["base_commit"] != current_base:
            raise ExecutionError("target repository or HEAD changed since run creation")
        if run.state != RunState.WAITING_FOR_APPROVAL:
            raise ExecutionError(f"run is {run.state}, not WAITING_FOR_APPROVAL")
        if not has_plan_execution_approval(run, store.get_approvals(run_id)):
            raise ExecutionError("matching PLAN_EXECUTION approval is required before provisioning")
        plan = store.get_plan(run_id)
        expected = {
            task.task_id: (task.assigned_provider, task.selected_model, TaskState.PENDING)
            for task in plan.tasks
        }
        actual = {
            str(row["task_id"]): (row["provider"], row["model"], row["state"])
            for row in store.list_task_runs(run_id)
        }
        if actual != expected:
            raise ExecutionError(
                "persisted tasks differ from the current plan or have prior attempts; create a new run"
            )
        store.transition_run(run_id, RunState.PROVISIONING)
        if observer is not None:
            observer.run_state(RunState.PROVISIONING.value)
        try:
            config = load_config(root)
            capabilities = {
                str(row["provider"]): row for row in await inspect_capabilities(root, config)
            }
            adapters: dict[str, CLIAdapter] = {}
            for task in plan.tasks:
                provider = config.providers.get(task.assigned_provider)
                cap = capabilities.get(task.assigned_provider)
                if provider is None or cap is None or not cap["available"]:
                    raise ExecutionError(
                        f"selected provider {task.assigned_provider} unavailable: {cap['reason'] if cap else 'not configured'}"
                    )
                executable = resolve_executable(root, provider.executable)
                if executable is None:
                    raise ExecutionError(
                        f"selected provider {task.assigned_provider} executable unavailable"
                    )
                adapters[task.task_id] = ADAPTERS[task.assigned_provider](executable)
                preview = AdapterRequest(
                    run_id,
                    task.task_id,
                    task.assigned_provider,
                    task.selected_model,
                    task.selected_role,
                    root / ".patchfleet" / "worktrees",
                    task.title,
                    task.purpose,
                    task.allowed_paths,
                    task.acceptance_criteria,
                    task.budget_limits,
                )
                adapters[task.task_id].build_command(preview)
        except (ConfigError, ExecutionError, AdapterUnavailable) as error:
            for task in plan.tasks:
                store.block_task(run_id, task.task_id, str(error))
                if observer is not None:
                    observer.task_state(task.task_id, TaskState.BLOCKED.value, 0, str(error))
            store.transition_run(run_id, RunState.BLOCKED)
            if observer is not None:
                observer.run_state(RunState.BLOCKED.value)
            raise ExecutionError(str(error)) from error
        store.transition_run(run_id, RunState.RUNNING)
        if observer is not None:
            observer.run_state(RunState.RUNNING.value)
        baseline = primary_snapshot(root)
        task_by_id = {task.task_id: task for task in plan.tasks}
        while True:
            states = _task_states(store, run_id)
            while dependents := blocked_dependents(plan, states):
                for task_id in dependents:
                    store.block_task(run_id, task_id, "prerequisite did not succeed")
                    if observer is not None:
                        observer.task_state(
                            task_id, TaskState.BLOCKED.value, 0, "prerequisite did not succeed"
                        )
                states = _task_states(store, run_id)
            ready = ready_tasks(plan, states, config.execution.max_parallel_workers)
            if not ready:
                break
            prepared = []
            for task_id in ready:
                try:
                    info = provision(root, run_id, task_id, current_base)
                    store.record_worktree(
                        run_id, task_id, info.worktree_path, info.branch, info.base_commit
                    )
                except (WorktreeError, StoreError) as error:
                    store.block_task(run_id, task_id, str(error))
                    if observer is not None:
                        observer.task_state(task_id, TaskState.BLOCKED.value, 0, str(error))
                    continue
                prepared.append((task_by_id[task_id], info, adapters[task_id]))
                if observer is not None:
                    observer.worktree(task_id, str(info.worktree_path))
                    observer.task_state(task_id, TaskState.PROVISIONED.value, 0)
            try:
                await asyncio.gather(
                    *(
                        _execute_task(
                            store,
                            run_id,
                            task,
                            info,
                            adapter,
                            baseline,
                            observer,
                            cancel_event,
                            (task_cancel_events or {}).get(task.task_id),
                        )
                        for task, info, adapter in prepared
                    )
                )
            except asyncio.CancelledError:
                store.transition_run(run_id, RunState.CANCELLED)
                if observer is not None:
                    observer.run_state(RunState.CANCELLED.value)
                raise
        states = _task_states(store, run_id)
        if cancel_event is not None and cancel_event.is_set():
            store.transition_run(run_id, RunState.CANCELLED)
        elif all(state == TaskState.SUCCEEDED for state in states.values()):
            store.transition_run(run_id, RunState.VERIFYING)
        elif any(state in {TaskState.FAILED, TaskState.CANCELLED} for state in states.values()):
            store.transition_run(run_id, RunState.FAILED)
        else:
            store.transition_run(run_id, RunState.BLOCKED)
        final = store.get_run(run_id).state
        if observer is not None:
            observer.run_state(final.value)
        return final
