"""Phase 4C: live catalogs, fleet view models, handoff, and TUI.

Every automated test uses fake local executables. No real provider CLI, account,
network call, or paid model invocation occurs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from patchfleet import catalog, execution, handoff, planning
from patchfleet.catalog_contracts import (
    DiscoveredModel,
    ModelCatalog,
    ModelCatalogSource,
    ModelCatalogStatus,
)
from patchfleet.contracts import RunState, plan_fingerprint
from patchfleet.discovery import ProviderStatus
from patchfleet.fleet import FleetController, FleetError
from patchfleet.handoff import HandoffError, approve_plan
from patchfleet.leader_contracts import LeaderPlanDraft
from patchfleet.monitor import FleetMonitor, page_count, worker_layout
from patchfleet.preferences import load_preferences
from patchfleet.settings import resolve_settings
from patchfleet.storage import SQLiteStore
from patchfleet.validation import validate_plan

# ---------------------------------------------------------------------------
# fixtures and fakes
# ---------------------------------------------------------------------------


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


def model_entry(
    model_id: str,
    display: str,
    *,
    efforts: tuple[str, ...] = ("low", "medium", "high"),
    default: str = "medium",
    hidden: bool = False,
) -> dict:
    return {
        "id": f"catalog-{model_id}",
        "model": model_id,
        "displayName": display,
        "description": f"{display} description",
        "hidden": hidden,
        "isDefault": default == "medium",
        "defaultReasoningEffort": default,
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort, "description": effort} for effort in efforts
        ],
    }


def fake_codex_app_server(
    path: Path,
    *,
    pages: list[dict] | None = None,
    mode: str = "ok",
    log: Path | None = None,
) -> Path:
    pages = (
        pages
        if pages is not None
        else [
            {"data": [model_entry("gpt-6-astra", "GPT-6 Astra")], "nextCursor": "page2"},
            {"data": [model_entry("gpt-6-sol", "GPT-6 Sol")], "nextCursor": None},
        ]
    )
    script = f"""#!{sys.executable}
import json
import os
import signal
import sys
import time

pages = {pages!r}
mode = {mode!r}
log = {str(log) if log is not None else None!r}


def record(payload):
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\\n")


def _terminate(*_args):
    record({{"exited": True}})
    sys.exit(0)


signal.signal(signal.SIGTERM, _terminate)
record({{"pid": os.getpid()}})

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        message = json.loads(raw)
    except Exception:
        continue
    record(message)
    method = message.get("method")
    if method == "initialize":
        if mode == "malformed":
            sys.stdout.write("this is not json\\n")
            sys.stdout.flush()
            continue
        if mode == "timeout":
            time.sleep(30)
            continue
        response = {{"jsonrpc": "2.0", "id": message["id"], "result": {{"userAgent": "fake"}}}}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
    elif method == "initialized":
        continue
    elif method == "model/list":
        cursor = (message.get("params") or {{}}).get("cursor")
        if cursor is None:
            page = pages[0]
        elif len(pages) > 1:
            page = pages[1]
        else:
            page = {{"data": [], "nextCursor": None}}
        response = {{"jsonrpc": "2.0", "id": message["id"], "result": page}}
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
record({{"exited": True}})
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_opencode(path: Path, *, mode: str = "ok") -> Path:
    script = f"""#!{sys.executable}
import sys

mode = {mode!r}
if mode == "fail":
    sys.stderr.write("boom\\n")
    sys.exit(3)
if mode == "empty":
    sys.exit(0)
print("opencode/big-pickle")
print(json.dumps({{
    "id": "big-pickle",
    "providerID": "opencode",
    "name": "Big Pickle",
    "family": "big-pickle",
    "status": "active",
    "variants": {{"low": {{"reasoningEffort": "low"}}, "high": {{"reasoningEffort": "high"}}}},
}}, indent=2))
print("opencode/deprecated-model")
print(json.dumps({{
    "id": "deprecated-model",
    "providerID": "opencode",
    "name": "Deprecated",
    "status": "deprecated",
    "variants": {{}},
}}, indent=2))
""".replace("json.dumps", "json.dumps")
    # json is imported lazily via the script below
    script = script.replace("import sys\n", "import json\nimport sys\n", 1)
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_claude(path: Path) -> Path:
    path.write_text(
        f"""#!{sys.executable}
import sys
if "--version" in sys.argv:
    print("claude-fake 1.0")
elif "--help" in sys.argv:
    print("Usage: claude [options] [command] [prompt]")
    print("  -p, --print   Print response and exit")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# Part A: provider-owned live catalogs
# ---------------------------------------------------------------------------


def test_codex_catalog_handshake_pagination_and_shutdown(tmp_path: Path) -> None:
    log = tmp_path / "app-server.jsonl"
    executable = fake_codex_app_server(tmp_path / "codex", log=log)

    result = asyncio.run(catalog.fetch_catalog("codex-cli", executable, timeout=10))

    assert result.status == ModelCatalogStatus.OK
    assert result.verified_available is True
    assert result.pages == 2
    assert [model.model_id for model in result.visible_models()] == [
        "gpt-6-astra",
        "gpt-6-sol",
    ]
    assert result.visible_models()[0].reasoning_efforts == ("low", "medium", "high")
    assert result.visible_models()[0].default_reasoning_effort == "medium"
    messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    methods = [message.get("method") for message in messages if "method" in message]
    assert methods[0] == "initialize"
    assert "initialized" in methods
    assert methods.count("model/list") == 2
    list_calls = [message for message in messages if message.get("method") == "model/list"]
    assert list_calls[0]["params"]["cursor"] is None
    assert list_calls[1]["params"]["cursor"] == "page2"
    assert any(message.get("exited") for message in messages)


def test_codex_catalog_malformed_output(tmp_path: Path) -> None:
    executable = fake_codex_app_server(tmp_path / "codex", mode="malformed")

    result = asyncio.run(catalog.fetch_catalog("codex-cli", executable, timeout=5))

    assert result.status == ModelCatalogStatus.ERROR
    assert result.verified_available is False
    assert result.models == ()


def test_codex_catalog_timeout(tmp_path: Path) -> None:
    executable = fake_codex_app_server(tmp_path / "codex", mode="timeout")

    result = asyncio.run(catalog.fetch_catalog("codex-cli", executable, timeout=0.5))

    assert result.status == ModelCatalogStatus.ERROR
    assert any("timed out" in diagnostic for diagnostic in result.diagnostics)


def test_codex_catalog_cancellation(tmp_path: Path) -> None:
    executable = fake_codex_app_server(tmp_path / "codex", mode="timeout")

    async def scenario():
        event = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, event.set)
        return await catalog.fetch_catalog("codex-cli", executable, timeout=5, cancel_event=event)

    result = asyncio.run(scenario())

    assert result.status == ModelCatalogStatus.ERROR
    assert any("cancelled" in diagnostic for diagnostic in result.diagnostics)


def test_opencode_catalog_verbose_parsing(tmp_path: Path) -> None:
    executable = fake_opencode(tmp_path / "opencode")

    result = asyncio.run(catalog.fetch_catalog("opencode", executable, timeout=10))

    assert result.status == ModelCatalogStatus.OK
    models = {model.model_id: model for model in result.models}
    assert set(models) == {"opencode/big-pickle", "opencode/deprecated-model"}
    assert models["opencode/big-pickle"].display_name == "Big Pickle"
    assert models["opencode/big-pickle"].reasoning_efforts == ("low", "high")
    assert models["opencode/deprecated-model"].hidden is True
    assert [model.model_id for model in result.visible_models()] == ["opencode/big-pickle"]


def test_opencode_catalog_unavailable_on_failure(tmp_path: Path) -> None:
    executable = fake_opencode(tmp_path / "opencode", mode="fail")

    result = asyncio.run(catalog.fetch_catalog("opencode", executable, timeout=10))

    assert result.status == ModelCatalogStatus.ERROR
    assert result.models == ()


def test_claude_catalog_reports_unavailable_without_static_list(tmp_path: Path) -> None:
    executable = fake_claude(tmp_path / "claude")

    result = asyncio.run(catalog.fetch_catalog("claude-code", executable, timeout=10))

    assert result.status == ModelCatalogStatus.UNAVAILABLE
    assert result.verified_available is False
    assert result.models == ()
    assert any("does not document" in diagnostic for diagnostic in result.diagnostics)


def test_no_hard_coded_fallback_catalog(tmp_path: Path) -> None:
    executable = fake_opencode(tmp_path / "opencode", mode="empty")

    result = asyncio.run(catalog.fetch_catalog("opencode", executable, timeout=10))

    assert result.status == ModelCatalogStatus.UNAVAILABLE
    assert result.models == ()

    unknown = asyncio.run(catalog.fetch_catalog("mystery-provider", tmp_path / "absent", timeout=1))
    assert unknown.status == ModelCatalogStatus.UNAVAILABLE
    assert unknown.models == ()


# ---------------------------------------------------------------------------
# shared helpers for Parts B and C
# ---------------------------------------------------------------------------


def make_status(
    provider_id: str,
    *,
    installed: bool = True,
    worker: bool = False,
    leader: bool = False,
    worker_ready: bool | None = None,
    leader_ready: bool | None = None,
) -> ProviderStatus:
    return ProviderStatus(
        provider_id=provider_id,
        display_name=provider_id,
        executable=f"/fake/{provider_id}",
        installed=installed,
        version="v1" if installed else None,
        execution_adapter=worker,
        adapter_ready=worker if worker_ready is None else worker_ready,
        detection_only=not (worker or leader),
        reason=None if installed else "not found",
        leader_adapter=leader,
        leader_ready=leader if leader_ready is None else leader_ready,
    )


def ok_catalog(
    provider_id: str = "codex-cli",
    models: tuple[str, ...] = ("leader-model", "worker-model"),
    *,
    source: ModelCatalogSource = ModelCatalogSource.CODEX_APP_SERVER,
) -> ModelCatalog:
    return ModelCatalog(
        provider_id=provider_id,
        status=ModelCatalogStatus.OK,
        source=source,
        discovered_at=datetime.now(UTC),
        verified_available=True,
        models=tuple(DiscoveredModel(model_id=model, display_name=model) for model in models),
        pages=1,
    )


def fleet_plan_data(
    *,
    tasks: tuple[tuple[str, tuple[str, ...]], ...] = (("T1", ()), ("T2", ())),
    leader_model: str = "leader-model",
    worker_model: str = "worker-model",
) -> dict:
    return {
        "schema_version": "0.1",
        "plan_id": "plan-1",
        "title": "Build the tasks API",
        "purpose": "Expose bounded task endpoints.",
        "leader": {
            "assigned_provider": "codex-cli",
            "selected_model": leader_model,
            "selected_role": "leader",
        },
        "reviewer": {
            "assigned_provider": "codex-cli",
            "selected_model": worker_model,
            "selected_role": "reviewer",
        },
        "tasks": [
            {
                "task_id": task_id,
                "title": f"Task {task_id}",
                "purpose": "Do bounded work.",
                "dependencies": list(dependencies),
                "assigned_provider": "codex-cli",
                "selected_model": worker_model,
                "selected_role": "worker",
                "allowed_paths": ["out.txt"],
                "acceptance_criteria": ["Writes only out.txt."],
                "test_commands": ["python -m pytest"],
                "budget_limits": {
                    "max_wall_time_seconds": 900,
                    "max_attempts": 1,
                    "max_output_bytes": 1000000,
                },
                "reviewer": {
                    "assigned_provider": "codex-cli",
                    "selected_model": worker_model,
                    "selected_role": "reviewer",
                },
            }
            for task_id, dependencies in tasks
        ],
    }


WORKER_MARKER = "WORKER_STREAM_MARKER_XYZ"


def fake_worker_cli(path: Path, *, behavior: str = "success") -> Path:
    script = f"""#!{sys.executable}
import pathlib
import sys
import time

if "--version" in sys.argv:
    print("fake-worker 1.0")
elif "--help" in sys.argv:
    print("exec --model --sandbox --print --permission-mode")
else:
    if {behavior!r} == "slow":
        time.sleep(10)
    sys.stdout.write({WORKER_MARKER!r} + "\\n")
    sys.stdout.flush()
    pathlib.Path("out.txt").write_text("changed")
    if {behavior!r} == "fail":
        sys.exit(7)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def write_project_config(repo: Path, executable: Path, *, maximum: int = 2) -> None:
    directory = repo / ".patchfleet"
    directory.mkdir(exist_ok=True)
    (directory / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "execution": {"max_parallel_workers": maximum},
                "providers": {"codex-cli": {"enabled": True, "executable": str(executable)}},
            }
        ),
        encoding="utf-8",
    )


def event_log_text(repo: Path) -> str:
    directory = repo / ".patchfleet" / "events"
    if not directory.is_dir():
        return ""
    return "\n".join(path.read_text(encoding="utf-8") for path in directory.glob("*.jsonl"))


# ---------------------------------------------------------------------------
# Part A: capabilities and contract strictness
# ---------------------------------------------------------------------------


def test_provider_capabilities_distinguish_roles_and_catalog() -> None:
    statuses = (
        make_status("codex-cli", worker=True, leader=True),
        make_status("claude-code", worker=True),
        make_status("opencode"),
    )
    capabilities = catalog.provider_capabilities(statuses, {"codex-cli": ok_catalog()})
    by_id = {item.provider_id: item for item in capabilities}

    assert by_id["codex-cli"].leader_capable and by_id["codex-cli"].worker_capable
    assert by_id["codex-cli"].catalog_available is True
    assert by_id["claude-code"].worker_capable and not by_id["claude-code"].leader_capable
    assert by_id["claude-code"].catalog_available is False
    assert by_id["opencode"].worker_capable is False and by_id["opencode"].leader_capable is False
    assert by_id["opencode"].catalog_status == ModelCatalogStatus.UNAVAILABLE


def test_verified_catalog_requires_models() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelCatalog(
            provider_id="codex-cli",
            status=ModelCatalogStatus.OK,
            source=ModelCatalogSource.CODEX_APP_SERVER,
            discovered_at=datetime.now(UTC),
            verified_available=True,
            models=(),
        )
    with pytest.raises(ValidationError):
        ModelCatalog(
            provider_id="codex-cli",
            status=ModelCatalogStatus.UNAVAILABLE,
            source=ModelCatalogSource.CODEX_APP_SERVER,
            discovered_at=datetime.now(UTC),
            verified_available=True,
            models=(DiscoveredModel(model_id="x", display_name="X"),),
        )


# ---------------------------------------------------------------------------
# Part B: layout and settings flow
# ---------------------------------------------------------------------------


def test_worker_layout_one_twoandmany() -> None:
    assert worker_layout(1, width=120, height=40).columns == 1
    assert worker_layout(2, width=120, height=40).columns == 2
    assert worker_layout(2, width=120, height=40).rows == 1
    assert worker_layout(3, width=120, height=40).columns == 2
    assert worker_layout(4, width=120, height=40).columns == 2
    assert worker_layout(4, width=120, height=40).rows == 2
    many = worker_layout(6, width=120, height=40)
    assert many.paged is True and many.capacity() == 4
    assert page_count(6, many) == 2
    compact = worker_layout(3, width=60, height=15)
    assert compact.compact is True and compact.columns == 1
    assert worker_layout(0, width=120, height=40).columns == 0


def make_controller(
    tmp_path: Path, repository: Path, *, catalog_models=("leader-model", "worker-model")
):
    statuses = (
        make_status("codex-cli", worker=True, leader=True),
        make_status("claude-code", worker=True),
        make_status("opencode"),
    )
    settings = resolve_settings(repository, None)
    fetcher = lambda provider_id, refresh: ok_catalog(  # noqa: E731
        provider_id, catalog_models
    )
    controller = FleetController(
        repository=repository,
        statuses=statuses,
        settings=settings,
        preferences_path=tmp_path / "preferences.yaml",
        catalog_fetcher=fetcher,
    )
    return controller


def test_settings_flow_rejects_free_form_and_reserved(tmp_path: Path, repository: Path) -> None:
    controller = make_controller(tmp_path, repository)
    controller.fetch_catalog("codex-cli")

    with pytest.raises(FleetError):
        controller.select_leader("codex-cli", "done")
    with pytest.raises(FleetError):
        controller.select_leader("codex-cli", "my-own-invented-model")
    with pytest.raises(FleetError):
        controller.select_leader("claude-code", "leader-model")  # not Leader-capable
    with pytest.raises(FleetError):
        controller.add_worker("opencode", "leader-model")  # detection only

    selected = controller.select_leader("codex-cli", "leader-model")
    assert selected.catalog_source == ModelCatalogSource.CODEX_APP_SERVER.value
    assert selected.catalog_discovered_at is not None
    controller.add_worker("codex-cli", "worker-model")
    controller.set_max_workers(2)
    path = controller.save_preferences()
    stored = load_preferences(path)
    assert stored is not None
    assert stored.leader.model == "leader-model"
    assert stored.leader.catalog_source == ModelCatalogSource.CODEX_APP_SERVER.value
    assert stored.workers[0].model == "worker-model"


def test_model_disappearing_blocks_new_execution(tmp_path: Path, repository: Path) -> None:
    controller = make_controller(tmp_path, repository)
    controller.fetch_catalog("codex-cli")
    controller.select_leader("codex-cli", "leader-model")
    controller.add_worker("codex-cli", "worker-model")
    assert controller.can_start_new_execution() == (True, "")

    controller.catalogs["codex-cli"] = ok_catalog("codex-cli", ("leader-model",))
    ready, reason = controller.can_start_new_execution()
    assert ready is False
    assert "missing from a fresh catalog" in reason

    controller.catalogs["codex-cli"] = ok_catalog("codex-cli", ("other-model",))
    with pytest.raises(FleetError):
        controller.start_request("Build something")


# ---------------------------------------------------------------------------
# Part C: handoff, approval guards, execution, and monitor
# ---------------------------------------------------------------------------


def test_handoff_creates_exact_approved_run(repository: Path) -> None:
    plan = validate_plan(fleet_plan_data())
    approved = approve_plan(plan, repository, "reviewer")

    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        run = store.get_run(approved.run_id)
        assert run.state == RunState.WAITING_FOR_APPROVAL
        assert run.plan_fingerprint == plan_fingerprint(plan)
        approvals = store.get_approvals(approved.run_id)
        assert [record.approval_type.value for record in approvals] == ["PLAN_EXECUTION"]
        assert approvals[0].subject_fingerprint == plan_fingerprint(plan)


def test_start_requires_exact_approval(repository: Path) -> None:
    plan = validate_plan(fleet_plan_data())
    run_id = execution.create_run(plan, repository)
    with pytest.raises(execution.ExecutionError, match="approval"):
        asyncio.run(execution.start_run(run_id, repository))


def test_handoff_rejects_invalid_plan(repository: Path) -> None:
    from patchfleet.contracts import Plan

    data = fleet_plan_data(tasks=(("T1", ("missing",)),))
    structurally_valid = Plan.model_validate(data)

    with pytest.raises(HandoffError):
        approve_plan(structurally_valid, repository, "actor")


def test_fleet_runs_two_workers_with_live_output_not_persisted(
    repository: Path, tmp_path: Path
) -> None:
    fake = fake_worker_cli(tmp_path / "worker")
    write_project_config(repository, fake, maximum=2)
    plan = validate_plan(fleet_plan_data())
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")
    mon.attach_run(approved.run_id)
    mon.mark_approved()

    final = handoff.start_fleet(approved, mon)

    assert final == "VERIFYING"
    snapshot = mon.snapshot()
    assert snapshot.run_state == "VERIFYING"
    assert len(snapshot.workers) == 2
    assert {worker.task_id for worker in snapshot.workers} == {"T1", "T2"}
    for worker in snapshot.workers:
        assert worker.state == "SUCCEEDED"
        assert any(WORKER_MARKER in line for line in worker.output)
        assert worker.worktree is not None

    logs = event_log_text(repository)
    assert WORKER_MARKER not in logs
    with SQLiteStore(repository / ".patchfleet", read_only=True) as store:
        for task in store.list_task_runs(approved.run_id):
            for attempt in store.list_attempts(str(task["task_run_id"])):
                assert "stdout" not in attempt
                assert "output" not in attempt


def test_fleet_failure_blocks_dependent(repository: Path, tmp_path: Path) -> None:
    fake = fake_worker_cli(tmp_path / "worker", behavior="fail")
    write_project_config(repository, fake)
    plan = validate_plan(fleet_plan_data(tasks=(("T1", ()), ("T2", ("T1",)))))
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")
    mon.attach_run(approved.run_id)

    final = handoff.start_fleet(approved, mon)

    assert final == "FAILED"
    states = {worker.task_id: worker.state for worker in mon.snapshot().workers}
    assert states["T1"] == "FAILED"
    assert states["T2"] == "BLOCKED"


def test_worker_timeout_is_reported(repository: Path, tmp_path: Path) -> None:
    fake = fake_worker_cli(tmp_path / "worker", behavior="slow")
    write_project_config(repository, fake)
    data = fleet_plan_data(tasks=(("T1", ()),))
    data["tasks"][0]["budget_limits"]["max_wall_time_seconds"] = 1
    plan = validate_plan(data)
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")

    final = handoff.start_fleet(approved, mon)

    assert final == "FAILED"
    worker = mon.snapshot().workers[0]
    assert worker.state == "FAILED"
    assert worker.reason is not None and "timed out" in worker.reason


def test_whole_run_cancellation(repository: Path, tmp_path: Path) -> None:
    fake = fake_worker_cli(tmp_path / "worker", behavior="slow")
    write_project_config(repository, fake)
    plan = validate_plan(fleet_plan_data(tasks=(("T1", ()), ("T2", ()))))
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")

    async def scenario() -> str:
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.4, cancel.set)
        return await handoff.start_fleet_async(approved, mon, cancel_event=cancel)

    final = asyncio.run(scenario())

    assert final == "CANCELLED"
    assert mon.snapshot().run_state == "CANCELLED"


def test_per_task_cancellation(repository: Path, tmp_path: Path) -> None:
    fake = fake_worker_cli(tmp_path / "worker", behavior="slow")
    write_project_config(repository, fake)
    plan = validate_plan(fleet_plan_data(tasks=(("T1", ()), ("T2", ()))))
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")
    events = {"T1": threading.Event()}
    events["T1"].set()

    handoff.start_fleet(approved, mon, task_cancel_events=events)

    states = {worker.task_id: worker.state for worker in mon.snapshot().workers}
    assert states["T1"] == "CANCELLED"
    assert states["T2"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Part C: end-to-end controller flow and TUI smoke
# ---------------------------------------------------------------------------


class ScriptedRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, session: planning.PlanningSession) -> object:
        self.calls += 1
        return self.responses.pop(0)


def make_draft(
    *, leader_model: str = "leader-model", worker_model: str = "worker-model"
) -> LeaderPlanDraft:
    payload = {
        "schema_version": "0.1",
        "outcome": "plan_draft",
        "message": "Here is a plan.",
        "assumptions": ["Tasks are private."],
        "risks": ["No persistence."],
        "task_rationale": [
            {"task_id": "T1", "rationale": "core"},
            {"task_id": "T2", "rationale": "tests"},
        ],
        "worker_usage": "Two Workers implement the API.",
        "proposed_plan": fleet_plan_data(leader_model=leader_model, worker_model=worker_model),
    }
    return LeaderPlanDraft.model_validate(payload)


def test_controller_end_to_end_with_fake_catalog_and_workers(
    repository: Path, tmp_path: Path
) -> None:
    fake = fake_worker_cli(tmp_path / "worker")
    write_project_config(repository, fake, maximum=2)
    controller = make_controller(tmp_path, repository)
    controller.leader_runner = ScriptedRunner([make_draft()])
    controller.fetch_catalog("codex-cli")
    controller.select_leader("codex-cli", "leader-model")
    controller.add_worker("codex-cli", "worker-model")
    controller.set_max_workers(2)

    controller.start_request("Build a small REST API")
    assert controller.state == "PLAN_READY"
    assert controller.plan is not None

    approved = controller.approve("reviewer")
    assert approved.plan_fingerprint == plan_fingerprint(controller.plan)
    controller.start_fleet()
    assert controller._run_thread is not None
    controller._run_thread.join(timeout=30)

    assert controller.state == "FINISHED"
    snapshot = controller.snapshot()
    assert snapshot is not None
    assert snapshot.run_state == "VERIFYING"
    assert len(snapshot.workers) == 2
    assert WORKER_MARKER not in event_log_text(repository)


def test_tui_smoke_displays_two_workers_and_verifying(repository: Path, tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from patchfleet.tui import FleetScreen, PatchFleetApp

    fake = fake_worker_cli(tmp_path / "worker")
    write_project_config(repository, fake, maximum=2)
    app_server = fake_codex_app_server(tmp_path / "codex-app-server")
    statuses = (make_status("codex-cli", worker=True, leader=True),)
    settings = resolve_settings(repository, None)

    def fetcher(provider_id: str, refresh: bool):
        return catalog.fetch_catalog_sync("codex-cli", app_server, timeout=10)

    controller = FleetController(
        repository=repository,
        statuses=statuses,
        settings=settings,
        preferences_path=tmp_path / "preferences.yaml",
        catalog_fetcher=fetcher,
    )
    controller.leader_runner = ScriptedRunner(
        [make_draft(leader_model="gpt-6-astra", worker_model="gpt-6-sol")]
    )
    live_catalog = controller.fetch_catalog("codex-cli", refresh=True)
    assert [model.model_id for model in live_catalog.visible_models()] == [
        "gpt-6-astra",
        "gpt-6-sol",
    ]
    controller.select_leader("codex-cli", "gpt-6-astra")
    controller.add_worker("codex-cli", "gpt-6-sol")
    controller.start_request("Build a small REST API")
    controller.approve("reviewer")
    controller.start_fleet()
    assert controller._run_thread is not None
    controller._run_thread.join(timeout=30)
    assert controller.state == "FINISHED"

    async def scenario() -> None:
        app = PatchFleetApp(controller)
        app.controller.state = "RUNNING"
        async with app.run_test(size=(140, 45)) as pilot:
            screen = FleetScreen(controller)
            await app.push_screen(screen)
            for _ in range(20):
                await pilot.pause()
                if screen._panel_ids:
                    break
            await pilot.pause()
            texts = [str(screen.query_one(f"#worker-panel-{index}").render()) for index in range(2)]
            assert "T1" in texts[0] and "T2" in texts[1]
            assert "SUCCEEDED" in texts[0] and "SUCCEEDED" in texts[1]
            assert "VERIFYING" in str(screen.query_one("#leader-panel").render())
            assert WORKER_MARKER in texts[0] or WORKER_MARKER in texts[1]

    asyncio.run(scenario())


def test_tui_setup_options_come_only_from_catalog(repository: Path, tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import OptionList

    from patchfleet.tui import PatchFleetApp, SetupScreen

    controller = make_controller(tmp_path, repository)
    controller.fetch_catalog("codex-cli")

    async def scenario() -> None:
        app = PatchFleetApp(controller)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = app.screen
            assert isinstance(screen, SetupScreen)
            providers = screen.query_one("#providers", OptionList)
            assert providers.option_count >= 3
            screen.provider_id = "codex-cli"
            screen._apply_models(controller.catalogs["codex-cli"])
            await pilot.pause()
            models = screen.query_one("#models", OptionList)
            ids = [models.get_option_at_index(index).id for index in range(models.option_count)]
            assert ids == ["leader-model", "worker-model"]

    asyncio.run(scenario())


def test_tui_quit_and_cancel_require_confirmation_when_running(
    repository: Path, tmp_path: Path
) -> None:
    pytest.importorskip("textual")
    from patchfleet.tui import ConfirmScreen, FleetScreen, PatchFleetApp

    controller = make_controller(tmp_path, repository)
    controller.monitor = FleetMonitor(str(repository), validate_plan(fleet_plan_data()))
    controller.monitor.run_state("RUNNING")

    async def scenario() -> None:
        app = PatchFleetApp(controller)
        app.controller.state = "RUNNING"
        async with app.run_test(size=(120, 40)) as pilot:
            screen = FleetScreen(controller)
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_request_quit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            app.screen.dismiss(False)
            await pilot.pause()
            assert isinstance(app.screen, FleetScreen)
            screen.action_cancel_run()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)

    asyncio.run(scenario())


def test_provision_failure_with_observer_blocks_without_worktree_callback(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_worker_cli(tmp_path / "worker")
    write_project_config(repository, fake)
    plan = validate_plan(fleet_plan_data(tasks=(("T1", ()),)))
    mon = FleetMonitor(str(repository), plan)
    approved = approve_plan(plan, repository, "reviewer")

    def fail_provision(*args: object, **kwargs: object):
        raise execution.WorktreeError("forced provisioning failure")

    monkeypatch.setattr(execution, "provision", fail_provision)

    final = handoff.start_fleet(approved, mon)

    assert final == "BLOCKED"
    worker = mon.snapshot().workers[0]
    assert worker.state == "BLOCKED"
    assert worker.worktree is None
    assert worker.reason is not None and "forced provisioning failure" in worker.reason
