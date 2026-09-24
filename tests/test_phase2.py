"""Phase 2 uses only fake CLIs and temporary Git repositories."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patchfleet.adapters.base import AdapterRequest, AdapterUnavailable
from patchfleet.adapters.claude_code import ClaudeCodeAdapter
from patchfleet.adapters.codex import CodexAdapter
from patchfleet.cli import app
from patchfleet.contracts import Plan, RunState
from patchfleet.execution import ExecutionError, approve_run, create_run, start_run
from patchfleet.processes import supervise
from patchfleet.scheduler import TaskState, blocked_dependents, ready_tasks
from patchfleet.storage import SQLiteStore, StoreError
from patchfleet.validation import validate_plan
from patchfleet.worktrees import WorktreeError, changed_paths, inspect_repository, provision


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "user.name", "PatchFleet Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "baseline")
    return repo


def fake_cli(path: Path, *, behavior: str = "success") -> None:
    script = f"""#!{sys.executable}
import pathlib
import sys
import time
if "--version" in sys.argv:
    print("fake-provider 1.0")
elif "--help" in sys.argv:
    print("exec --model --sandbox --print --permission-mode")
else:
    if {behavior!r} == "slow":
        time.sleep(2)
    if {behavior!r} == "outside":
        pathlib.Path("outside.txt").write_text("changed")
    else:
        pathlib.Path("out.txt").write_text("changed")
    if {behavior!r} == "noisy":
        sys.stdout.write("x" * 200000)
    if {behavior!r} == "fail":
        sys.exit(7)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def config(repo: Path, executable: Path, *, provider: str = "codex-cli", maximum: int = 2) -> None:
    directory = repo / ".patchfleet"
    directory.mkdir(exist_ok=True)
    (directory / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "execution": {"max_parallel_workers": maximum},
                "providers": {provider: {"enabled": True, "executable": str(executable)}},
            }
        ),
        encoding="utf-8",
    )


def worker_plan(plan_data: dict, *, provider: str = "codex-cli") -> Plan:
    plan_data["tasks"][0]["assigned_provider"] = provider
    plan_data["tasks"][0]["selected_model"] = "explicit-model"
    plan_data["tasks"][0]["allowed_paths"] = ["out.txt"]
    plan_data["tasks"][0]["budget_limits"]["max_wall_time_seconds"] = 1
    return validate_plan(plan_data)


def test_doctor_reports_missing_and_fake_executables(repository: Path) -> None:
    runner = CliRunner()
    missing_config = runner.invoke(app, ["doctor", "--repo", str(repository)])
    assert missing_config.exit_code != 0
    assert "missing configuration" in missing_config.stderr
    assert not (repository / ".patchfleet").exists()
    fake = repository.parent / "fake-provider"
    fake_cli(fake)
    config(repository, fake)
    available = runner.invoke(app, ["doctor", "--repo", str(repository)])
    assert available.exit_code == 0
    assert "fake-provider 1.0" in available.stdout
    assert "explicit model flag=True" in available.stdout
    config(repository, repository.parent / "absent")
    missing = runner.invoke(app, ["doctor", "--repo", str(repository)])
    assert missing.exit_code != 0
    assert "not found" in missing.stdout


def test_doctor_plan_reports_selected_model_without_remote_claim(
    repository: Path, plan_data: dict
) -> None:
    fake = repository.parent / "fake-provider"
    fake_cli(fake)
    config(repository, fake)
    path = repository.parent / "plan.yaml"
    path.write_text(
        yaml.safe_dump(worker_plan(plan_data).model_dump(mode="json")), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["doctor", "--repo", str(repository), "--plan", str(path)])
    assert result.exit_code == 0
    assert "model=explicit-model" in result.stdout
    assert "cannot be checked offline" in result.stdout


def test_adapter_commands_pin_model_and_provider(tmp_path: Path, plan_data: dict) -> None:
    task = worker_plan(plan_data).tasks[0]
    request = AdapterRequest(
        "run-1",
        "T1",
        task.assigned_provider,
        task.selected_model,
        task.selected_role,
        tmp_path,
        task.title,
        task.purpose,
        task.allowed_paths,
        task.acceptance_criteria,
        task.budget_limits,
    )
    codex = CodexAdapter(tmp_path / "codex")
    argv, prompt = codex.build_command(request)
    assert argv[argv.index("--model") + 1] == "explicit-model"
    assert argv[-1] == "-" and b"Allowed paths" in prompt
    with pytest.raises(AdapterUnavailable):
        ClaudeCodeAdapter(tmp_path / "claude").build_command(request)
    unsafe_model = AdapterRequest(**{**request.__dict__, "model": "--fallback-model"})
    with pytest.raises(AdapterUnavailable, match="safe CLI argument"):
        codex.build_command(unsafe_model)
    claude_request = AdapterRequest(**{**request.__dict__, "provider": "claude-code"})
    claude_argv, claude_stdin = ClaudeCodeAdapter(tmp_path / "claude").build_command(claude_request)
    assert claude_argv[claude_argv.index("--model") + 1] == "explicit-model"
    assert "--fallback-model" not in claude_argv
    assert claude_stdin is None


def test_approval_guard_precedes_worktree_and_launch(repository: Path, plan_data: dict) -> None:
    fake = repository.parent / "fake-provider"
    fake_cli(fake)
    config(repository, fake)
    run_id = create_run(worker_plan(plan_data), repository)
    with pytest.raises(ExecutionError, match="approval"):
        asyncio.run(start_run(run_id, repository))
    assert not (repository / ".patchfleet" / "worktrees").exists()
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        assert store.get_run(run_id).state == RunState.WAITING_FOR_APPROVAL
        assert store.list_attempts(f"{run_id}:T1") == []


def test_unconfigured_selected_provider_blocks_without_fallback(
    repository: Path, plan_data: dict
) -> None:
    fake = repository.parent / "fake-claude"
    fake_cli(fake)
    config(repository, fake, provider="claude-code")
    run_id = create_run(worker_plan(plan_data, provider="codex-cli"), repository)
    approve_run(run_id, "human", repository)
    with pytest.raises(ExecutionError, match="codex-cli unavailable"):
        asyncio.run(start_run(run_id, repository))
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        task = store.list_task_runs(run_id)[0]
        assert store.get_run(run_id).state == RunState.BLOCKED
        assert task["state"] == TaskState.BLOCKED
        assert store.list_attempts(str(task["task_run_id"])) == []
    assert not (repository / ".patchfleet" / "worktrees").exists()


@pytest.mark.parametrize(
    "behavior,expected",
    [("success", RunState.VERIFYING), ("fail", RunState.FAILED), ("outside", RunState.FAILED)],
)
def test_worker_isolated_and_persisted(
    repository: Path, plan_data: dict, behavior: str, expected: RunState
) -> None:
    fake = repository.parent / "fake-provider"
    fake_cli(fake, behavior=behavior)
    config(repository, fake)
    initial_head = git(repository, "rev-parse", "HEAD")
    run_id = create_run(worker_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    assert asyncio.run(start_run(run_id, repository)) == expected
    assert git(repository, "rev-parse", "HEAD") == initial_head
    assert git(repository, "status", "--porcelain") == ""
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        task = store.list_task_runs(run_id)[0]
        attempt = store.list_attempts(str(task["task_run_id"]))[0]
        assert Path(str(task["worktree_path"])).is_dir()
        assert task["branch"] and task["base_commit"] == initial_head
        assert task["provider"] == "codex-cli" and task["model"] == "explicit-model"
        assert attempt["attempt_number"] == 1
        assert attempt["exit_code"] == (7 if behavior == "fail" else 0)
        assert attempt["duration_seconds"] > 0
        if behavior == "outside":
            assert task["out_of_scope_paths"] == ["outside.txt"]
        else:
            assert task["changed_paths"] == ["out.txt"]
        assert all("prompt" not in event.payload for event in store.get_events(run_id))


def test_attempt_persists_truncation_metadata_not_raw_output(
    repository: Path, plan_data: dict
) -> None:
    fake = repository.parent / "noisy-provider"
    fake_cli(fake, behavior="noisy")
    config(repository, fake)
    plan_data["tasks"][0]["budget_limits"]["max_output_bytes"] = 32
    run_id = create_run(worker_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    assert asyncio.run(start_run(run_id, repository)) == RunState.VERIFYING
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        attempt = store.list_attempts(f"{run_id}:T1")[0]
        assert attempt["stdout_bytes"] == 200000
        assert attempt["output_truncated"] == 1
        assert "stdout" not in attempt
        assert all("output" not in event.payload for event in store.get_events(run_id))


def test_worktree_provisioning_does_not_touch_primary(repository: Path) -> None:
    root, base = inspect_repository(repository)
    info = provision(root, "run-1", "T1", base)
    (info.worktree_path / "out.txt").write_text("hello", encoding="utf-8")
    assert changed_paths(info) == ("out.txt",)
    assert not (repository / "out.txt").exists()
    assert git(repository, "rev-parse", "HEAD") == base


def test_worktree_refuses_symlinked_parent(repository: Path) -> None:
    root, base = inspect_repository(repository)
    metadata = root / ".patchfleet"
    metadata.mkdir()
    (metadata / "worktrees").symlink_to(repository.parent, target_is_directory=True)
    with pytest.raises(WorktreeError, match="symlink"):
        provision(root, "run-1", "T1", base)


def test_scheduler_order_limit_and_failed_dependency(plan_data: dict) -> None:
    first = plan_data["tasks"][0]
    first["task_id"] = "b"
    for task_id, dependencies in [("a", []), ("c", ["a"]), ("d", ["b"])]:
        plan_data["tasks"].append({**first, "task_id": task_id, "dependencies": dependencies})
    plan = validate_plan(plan_data)
    states = {task.task_id: TaskState.PENDING for task in plan.tasks}
    assert ready_tasks(plan, states, 1) == ("a",)
    assert ready_tasks(plan, states, 2) == ("a", "b")
    states["a"] = TaskState.FAILED
    assert blocked_dependents(plan, states) == ("c",)
    assert ready_tasks(plan, states, 2) == ("b",)


def test_dispatch_obeys_dependencies_and_parallel_limit(repository: Path, plan_data: dict) -> None:
    fake = repository.parent / "timed-provider"
    timeline = repository.parent / "timeline.txt"
    fake.write_text(
        f"""#!{sys.executable}
import pathlib
import sys
import time
if "--version" in sys.argv:
    print("fake 1")
elif "--help" in sys.argv:
    print("--model --sandbox")
else:
    task = pathlib.Path.cwd().name.split("-")[0]
    with open({str(timeline)!r}, "a") as stream:
        stream.write(f"{{task}} start {{time.monotonic()}}\\n")
    time.sleep(0.15)
    pathlib.Path("out.txt").write_text("done")
    with open({str(timeline)!r}, "a") as stream:
        stream.write(f"{{task}} end {{time.monotonic()}}\\n")
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    config(repository, fake, maximum=2)
    first = plan_data["tasks"][0]
    first["task_id"] = "a"
    first["allowed_paths"] = ["out.txt"]
    for task_id, dependencies in [("b", []), ("c", ["a", "b"])]:
        plan_data["tasks"].append({**first, "task_id": task_id, "dependencies": dependencies})
    run_id = create_run(validate_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    assert asyncio.run(start_run(run_id, repository)) == RunState.VERIFYING
    entries = [line.split() for line in timeline.read_text().splitlines()]
    stamps = {(task, event): float(instant) for task, event, instant in entries}
    assert stamps["c", "start"] >= max(stamps["a", "end"], stamps["b", "end"])
    assert stamps["a", "start"] < stamps["b", "end"]
    assert stamps["b", "start"] < stamps["a", "end"]


def test_failed_prerequisite_blocks_transitive_dependents(
    repository: Path, plan_data: dict
) -> None:
    fake = repository.parent / "failing-provider"
    fake_cli(fake, behavior="fail")
    config(repository, fake)
    first = plan_data["tasks"][0]
    first["task_id"] = "a"
    first["allowed_paths"] = ["out.txt"]
    for task_id, dependencies in [("b", ["a"]), ("c", ["b"])]:
        plan_data["tasks"].append({**first, "task_id": task_id, "dependencies": dependencies})
    run_id = create_run(validate_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    assert asyncio.run(start_run(run_id, repository)) == RunState.FAILED
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        tasks = {task["task_id"]: task for task in store.list_task_runs(run_id)}
        assert tasks["a"]["state"] == TaskState.FAILED
        assert tasks["b"]["state"] == tasks["c"]["state"] == TaskState.BLOCKED
        assert store.list_attempts(str(tasks["b"]["task_run_id"])) == []
        assert store.list_attempts(str(tasks["c"]["task_run_id"])) == []


def test_storage_refuses_to_provision_unmet_dependency(repository: Path, plan_data: dict) -> None:
    first = plan_data["tasks"][0]
    first["task_id"] = "a"
    first["allowed_paths"] = ["out.txt"]
    plan_data["tasks"].append({**first, "task_id": "b", "dependencies": ["a"]})
    run_id = create_run(validate_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    with SQLiteStore(repository / ".patchfleet") as store:
        store.transition_run(run_id, RunState.PROVISIONING)
        base = store.get_target(run_id)["base_commit"]
        with pytest.raises(StoreError, match="dependency a has not succeeded"):
            store.record_worktree(run_id, "b", repository / "wrong", "branch", base)
        assert store.list_task_runs(run_id)[1]["state"] == TaskState.PENDING


def test_process_timeout_cancellation_nonzero_and_truncation() -> None:
    async def checks() -> None:
        failure = await supervise(
            [sys.executable, "-c", "raise SystemExit(9)"], timeout=2, max_output_bytes=8
        )
        assert failure.status == "failed" and failure.exit_code == 9
        noise = await supervise(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*100000)",
            ],
            timeout=2,
            max_output_bytes=32,
        )
        assert noise.status == "succeeded" and noise.output_truncated
        assert len(noise.stdout) + len(noise.stderr) == 32
        timed = await supervise(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=0.05,
            max_output_bytes=8,
            termination_grace_seconds=0.05,
        )
        assert timed.timed_out and timed.status == "timed_out"
        event = asyncio.Event()

        async def cancel_soon() -> None:
            await asyncio.sleep(0.05)
            event.set()

        asyncio.create_task(cancel_soon())
        cancelled = await supervise(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=2,
            max_output_bytes=8,
            cancel_event=event,
            termination_grace_seconds=0.05,
        )
        assert cancelled.cancelled and cancelled.status == "cancelled"

    asyncio.run(checks())


def test_restart_marks_running_attempt_interrupted(repository: Path, plan_data: dict) -> None:
    fake = repository.parent / "fake-provider"
    fake_cli(fake)
    config(repository, fake)
    run_id = create_run(worker_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    with SQLiteStore(repository / ".patchfleet") as store:
        store.transition_run(run_id, RunState.PROVISIONING)
        base = store.get_target(run_id)["base_commit"]
        info = provision(repository, run_id, "T1", base)
        store.record_worktree(run_id, "T1", info.worktree_path, info.branch, base)
        store.transition_run(run_id, RunState.RUNNING)
        store.begin_attempt(run_id, "T1")
    with pytest.raises(ExecutionError, match="interrupted"):
        asyncio.run(start_run(run_id, repository))
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        assert store.get_run(run_id).state == RunState.BLOCKED
        assert store.list_task_runs(run_id)[0]["state"] == TaskState.INTERRUPTED
        assert store.list_attempts(f"{run_id}:T1")[0]["status"] == TaskState.INTERRUPTED


def test_phase2_event_mirror_reconciles_from_sqlite(repository: Path, plan_data: dict) -> None:
    fake = repository.parent / "fake-provider"
    fake_cli(fake)
    config(repository, fake)
    run_id = create_run(worker_plan(plan_data), repository)
    approve_run(run_id, "human", repository)
    asyncio.run(start_run(run_id, repository))
    directory = repository / ".patchfleet"
    log = directory / "events" / f"{run_id}.jsonl"
    original = log.read_bytes()
    log.write_bytes(original.rsplit(b"\n", 2)[0] + b'\n{"partial":')
    with SQLiteStore(directory) as store:
        events = store.get_events(run_id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert log.read_bytes() == original
    assert b"prompt" not in original and b"output" not in original


def test_cli_create_approve_status(repository: Path, plan_data: dict, tmp_path: Path) -> None:
    plan = worker_plan(plan_data)
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan.model_dump(mode="json")), encoding="utf-8")
    runner = CliRunner()
    created = runner.invoke(app, ["run", "create", str(path), "--repo", str(repository)])
    assert created.exit_code == 0
    run_id = created.stdout.split()[1].rstrip(":")
    status = runner.invoke(app, ["run", "status", run_id, "--repo", str(repository)])
    assert "WAITING_FOR_APPROVAL" in status.stdout
    assert "explicit-model" in status.stdout
    approved = runner.invoke(
        app, ["run", "approve", run_id, "--actor", "human", "--repo", str(repository)]
    )
    assert approved.exit_code == 0
