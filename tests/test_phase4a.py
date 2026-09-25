"""Phase 4A: interactive routing, preferences, discovery, and settings."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from patchfleet import cli, shell
from patchfleet import preferences as preferences_module
from patchfleet.cli import app
from patchfleet.discovery import ProviderStatus, discover_providers_sync
from patchfleet.leader_contracts import LeaderQuestions
from patchfleet.preferences import (
    PreferencesError,
    Selection,
    UiPreferences,
    UserPreferences,
    load_preferences,
    save_preferences,
)
from patchfleet.settings import (
    SOURCE_COMMAND,
    SOURCE_DEFAULT,
    SOURCE_PROJECT,
    SOURCE_USER,
    resolve_settings,
)
from patchfleet.shell import (
    ShellIO,
    WizardAnswers,
    build_preferences,
    run_interactive,
    run_shell,
)

CODEX_HELP = (
    "exec --model --sandbox read-only --print --permission-mode "
    "--output-schema --output-last-message"
)


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


def fake_cli(
    path: Path, *, version: str = "fake-provider 1.0", help_text: str = CODEX_HELP
) -> Path:
    path.write_text(
        f"""#!{sys.executable}
import sys
if "--version" in sys.argv:
    print({version!r})
elif "--help" in sys.argv:
    print({help_text!r})
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def status(
    provider_id: str,
    *,
    installed: bool = True,
    adapter: bool = True,
    adapter_ready: bool = True,
    leader: bool = False,
    leader_ready: bool = True,
    version: str | None = "fake 1.0",
) -> ProviderStatus:
    return ProviderStatus(
        provider_id=provider_id,
        display_name=provider_id,
        executable=f"/fake/{provider_id}",
        installed=installed,
        version=version if installed else None,
        execution_adapter=adapter,
        adapter_ready=adapter and adapter_ready,
        detection_only=not (adapter or leader),
        reason=None if installed else "not found",
        leader_adapter=leader,
        leader_ready=leader and leader_ready,
    )


STANDARD_STATUSES = (
    status("codex-cli", leader=True),
    status("claude-code", installed=False),
    status("opencode", adapter=False, adapter_ready=False),
)


def scripted_io(*responses: str, echo: list[str] | None = None) -> ShellIO:
    iterator = iter(responses)
    sink = echo if echo is not None else []

    def read_line(prompt: str) -> str:
        sink.append(prompt)
        return next(iterator)

    return ShellIO(read_line, sink.append, sink.append)


def valid_preferences(*, maximum: int = 2) -> UserPreferences:
    return UserPreferences(
        schema_version="0.1",
        provider_executables={"codex-cli": "/custom/codex"},
        leader=Selection(provider="codex-cli", model="leader-model"),
        workers=(Selection(provider="codex-cli", model="worker-model"),),
        max_parallel_workers=maximum,
        ui=UiPreferences(),
    )


def test_no_argument_non_interactive_prints_usage_and_exits_nonzero() -> None:
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 2
    assert "No interactive terminal was detected" in result.stderr
    assert "settings" in result.stdout
    assert "doctor" in result.stdout


def test_no_argument_interactive_routes_to_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake_interactive() -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr(cli, "run_interactive", fake_interactive)
    result = CliRunner().invoke(app, [])

    assert result.exit_code == 0
    assert called == [True]


def test_first_run_setup_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "preferences.yaml"
    io = scripted_io("n", "1", "leader-model", "1", "worker-model", "done", "3", "y")

    built = shell.run_first_run_setup(io, STANDARD_STATUSES, preferences_path=path)

    assert built is not None
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = load_preferences(path)
    assert reloaded == built
    assert reloaded.leader == Selection(provider="codex-cli", model="leader-model")
    assert reloaded.workers == (Selection(provider="codex-cli", model="worker-model"),)
    assert reloaded.max_parallel_workers == 3


def test_setup_cancellation_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    io = scripted_io("n", "q")

    built = shell.run_first_run_setup(io, STANDARD_STATUSES, preferences_path=path)

    assert built is None
    assert not path.exists()


def test_setup_confirmation_can_decline(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    io = scripted_io("n", "1", "leader-model", "1", "worker-model", "done", "1", "n")

    built = shell.run_first_run_setup(io, STANDARD_STATUSES, preferences_path=path)

    assert built is None
    assert not path.exists()


def test_malformed_preferences_raise_and_are_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    path.write_text("leader: [this is not valid\n", encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(PreferencesError):
        load_preferences(path)

    io = scripted_io("n")
    code = run_interactive(repository=None, io=io, preferences_path=path, detect=False)

    assert code == 1
    assert path.read_bytes() == original


def test_no_execution_capable_provider_leaves_preferences_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    only_detection = (
        status("codex-cli", installed=False),
        status("opencode", adapter=False, adapter_ready=False),
    )
    io = scripted_io("n")

    built = shell.run_first_run_setup(io, only_detection, preferences_path=path)

    assert built is None
    assert not path.exists()


def test_provider_discovery_with_fake_executables(tmp_path: Path) -> None:
    codex = fake_cli(tmp_path / "codex")
    opencode = fake_cli(tmp_path / "opencode", version="opencode 0.5", help_text="run --model")

    statuses = discover_providers_sync(
        {
            "codex-cli": str(codex),
            "claude-code": str(tmp_path / "absent-claude"),
            "opencode": str(opencode),
        },
        repository=tmp_path,
    )
    by_id = {item.provider_id: item for item in statuses}

    assert by_id["codex-cli"].installed and by_id["codex-cli"].execution_capable
    assert by_id["codex-cli"].version == "fake-provider 1.0"
    assert by_id["opencode"].installed
    assert by_id["opencode"].detection_only
    assert not by_id["opencode"].execution_capable
    assert not by_id["claude-code"].installed


def test_discovery_reports_installed_but_unusable_adapter(tmp_path: Path) -> None:
    codex = fake_cli(tmp_path / "codex", help_text="exec --sandbox")

    statuses = discover_providers_sync(
        {
            "codex-cli": str(codex),
            "claude-code": str(tmp_path / "absent"),
            "opencode": str(tmp_path / "absent"),
        },
        repository=tmp_path,
    )
    codex_status = next(item for item in statuses if item.provider_id == "codex-cli")

    assert codex_status.installed
    assert codex_status.execution_adapter
    assert not codex_status.adapter_ready
    assert not codex_status.execution_capable


def test_build_preferences_rejects_unknown_or_blank_selections() -> None:
    with pytest.raises(shell.WizardError):
        build_preferences(
            WizardAnswers(
                "opencode", "some-model", (Selection(provider="codex-cli", model="w"),), 1
            ),
            STANDARD_STATUSES,
        )
    with pytest.raises(shell.WizardError):
        build_preferences(
            WizardAnswers("codex-cli", "   ", (Selection(provider="codex-cli", model="w"),), 1),
            STANDARD_STATUSES,
        )
    with pytest.raises(shell.WizardError):
        build_preferences(
            WizardAnswers("codex-cli", "l", (Selection(provider="claude-code", model="m"),), 1),
            STANDARD_STATUSES,
        )
    with pytest.raises(shell.WizardError):
        build_preferences(
            WizardAnswers("codex-cli", "--evil", (Selection(provider="codex-cli", model="m"),), 1),
            STANDARD_STATUSES,
        )
    with pytest.raises(shell.WizardError):
        build_preferences(
            WizardAnswers("codex-cli", "l", (), 1),
            STANDARD_STATUSES,
        )


def test_preference_precedence_and_legacy_repository_config(repository: Path) -> None:
    metadata = repository / ".patchfleet"
    metadata.mkdir()
    (metadata / "config.yaml").write_text(
        "schema_version: '0.1'\n"
        "execution:\n  max_parallel_workers: 5\n"
        "providers:\n  codex-cli:\n    enabled: true\n    executable: /project/codex\n",
        encoding="utf-8",
    )
    prefs = valid_preferences(maximum=2)

    resolved = resolve_settings(repository, prefs)

    assert resolved.max_parallel_workers == 5
    assert resolved.max_parallel_workers_source == SOURCE_PROJECT
    assert resolved.provider_executables["codex-cli"] == "/project/codex"
    assert resolved.provider_executable_sources["codex-cli"] == SOURCE_PROJECT
    assert resolved.leader == prefs.leader and resolved.leader_source == SOURCE_USER
    assert resolved.workers == prefs.workers and resolved.workers_source == SOURCE_USER

    overridden = resolve_settings(repository, prefs, command_options={"max_parallel_workers": 7})
    assert overridden.max_parallel_workers == 7
    assert overridden.max_parallel_workers_source == SOURCE_COMMAND

    no_override = resolve_settings(None, prefs)
    assert no_override.max_parallel_workers == 2
    assert no_override.max_parallel_workers_source == SOURCE_USER
    assert no_override.provider_executables["codex-cli"] == "/custom/codex"
    assert no_override.provider_executable_sources["codex-cli"] == SOURCE_USER

    built_in = resolve_settings(None, None)
    assert built_in.max_parallel_workers_source == SOURCE_DEFAULT
    assert built_in.leader is None
    assert built_in.workers == ()


def test_project_config_error_does_not_crash_resolution(repository: Path) -> None:
    metadata = repository / ".patchfleet"
    metadata.mkdir()
    (metadata / "config.yaml").write_text("not: a: valid: config\n", encoding="utf-8")

    resolved = resolve_settings(repository, valid_preferences())

    assert resolved.project_config_error is not None
    assert resolved.max_parallel_workers == 2
    assert resolved.max_parallel_workers_source == SOURCE_USER


def test_settings_show_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "preferences.yaml"
    save_preferences(valid_preferences(), path)
    monkeypatch.setattr(preferences_module, "user_preferences_path", lambda: path)
    before = path.read_bytes()

    result = CliRunner().invoke(app, ["settings", "show", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "Leader: codex-cli / leader-model" in result.stdout
    assert "[user preferences]" in result.stdout
    assert "No credentials" in result.stdout
    assert path.read_bytes() == before
    assert not (tmp_path / ".patchfleet").exists()


def test_settings_show_handles_missing_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        preferences_module, "user_preferences_path", lambda: tmp_path / "absent.yaml"
    )

    result = CliRunner().invoke(app, ["settings", "show", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "not created" in result.stdout
    assert "not configured" in result.stdout


def test_settings_show_reports_malformed_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "preferences.yaml"
    path.write_text("leader: [unterminated\n", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(preferences_module, "user_preferences_path", lambda: path)

    result = CliRunner().invoke(app, ["settings", "show", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert "unreadable" in result.stdout
    assert path.read_bytes() == before


def test_settings_editor_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("patchfleet.settings_cli.is_interactive", lambda: False)
    result = CliRunner().invoke(app, ["settings"])

    assert result.exit_code == 2
    assert "interactive terminal" in result.stderr


def test_shell_commands_do_not_create_runs_approvals_or_worktrees(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs = valid_preferences()
    settings = resolve_settings(repository, prefs)
    path = tmp_path / "preferences.yaml"

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("shell must not discover or launch providers here")

    monkeypatch.setattr(shell, "discover_providers_sync", forbidden)
    calls: list[str] = []

    def fake_leader(session: object) -> LeaderQuestions:
        calls.append("called")
        return LeaderQuestions(
            schema_version="0.1",
            outcome="questions",
            message="A couple of questions first.",
            questions=("Should tasks be private per user?",),
        )

    captured: list[str] = []
    io = scripted_io("/help", "/new", "add a health endpoint", "/doctor", "/quit", echo=captured)
    code = run_shell(
        io,
        statuses=STANDARD_STATUSES,
        settings=settings,
        repository=repository,
        preferences_path=path,
        leader_runner=fake_leader,
    )

    assert code == 0
    assert calls == ["called"]
    assert not (repository / ".patchfleet").exists()
    assert not path.exists()
    output = "\n".join(captured)
    assert "Should tasks be private per user?" in output
    assert "no Worker, model, run, or approval was started" in output


def test_shell_settings_editor_can_cancel(repository: Path, tmp_path: Path) -> None:
    settings = resolve_settings(repository, valid_preferences())
    path = tmp_path / "preferences.yaml"
    captured: list[str] = []
    io = scripted_io("/settings", "n", "q", "/quit", echo=captured)

    code = run_shell(
        io,
        statuses=STANDARD_STATUSES,
        settings=settings,
        repository=repository,
        preferences_path=path,
    )

    assert code == 0
    assert not path.exists()
    assert not (repository / ".patchfleet").exists()
    assert any("PatchFleet settings editor" in line for line in captured)
    assert "No changes saved" in "\n".join(captured)


def test_run_interactive_first_run_then_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "preferences.yaml"
    monkeypatch.setattr(shell, "discover_providers_sync", lambda *a, **k: STANDARD_STATUSES)
    captured: list[str] = []
    io = scripted_io(
        "n",
        "1",
        "leader-model",
        "1",
        "worker-model",
        "done",
        "2",
        "y",
        "/new",
        "build it",
        "/quit",
        echo=captured,
    )

    code = run_interactive(repository=None, io=io, preferences_path=path, detect=False)

    assert code == 0
    assert path.is_file()
    assert "Planning needs a target Git repository" in "\n".join(captured)
    reloaded = load_preferences(path)
    assert reloaded.max_parallel_workers == 2


def test_shell_settings_refresh_updates_active_defaults(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = valid_preferences()
    settings = resolve_settings(repository, initial)
    path = tmp_path / "preferences.yaml"
    save_preferences(initial, path)
    monkeypatch.setattr(shell, "discover_providers_sync", lambda *a, **k: STANDARD_STATUSES)
    captured: list[str] = []
    io = scripted_io(
        "/settings",
        "n",
        "1",
        "new-leader",
        "1",
        "new-worker",
        "done",
        "4",
        "y",
        "/doctor",
        "/quit",
        echo=captured,
    )

    code = run_shell(
        io,
        statuses=STANDARD_STATUSES,
        settings=settings,
        repository=repository,
        preferences_path=path,
    )

    output = "\n".join(captured)
    assert code == 0
    assert "Active defaults updated for this session:" in output
    assert output.count("codex-cli / new-leader") >= 2
    assert "Maximum parallel Workers: 4" in output
    reloaded = load_preferences(path)
    assert reloaded.leader == Selection(provider="codex-cli", model="new-leader")
    assert reloaded.workers == (Selection(provider="codex-cli", model="new-worker"),)
    assert reloaded.max_parallel_workers == 4


def test_shell_settings_cancellation_preserves_active_settings(
    repository: Path, tmp_path: Path
) -> None:
    initial = valid_preferences()
    settings = resolve_settings(repository, initial)
    path = tmp_path / "preferences.yaml"
    save_preferences(initial, path)
    captured: list[str] = []
    io = scripted_io("/settings", "n", "q", "/doctor", "/quit", echo=captured)

    code = run_shell(
        io,
        statuses=STANDARD_STATUSES,
        settings=settings,
        repository=repository,
        preferences_path=path,
    )

    output = "\n".join(captured)
    assert code == 0
    assert "No changes saved" in output
    assert "Active defaults updated" not in output
    assert "Leader: codex-cli / leader-model" in output
    assert load_preferences(path) == initial
    assert not (repository / ".patchfleet").exists()


def test_shell_settings_save_failure_preserves_active_settings(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = valid_preferences()
    settings = resolve_settings(repository, initial)
    path = tmp_path / "preferences.yaml"
    save_preferences(initial, path)
    before = path.read_bytes()

    def fail_save(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(preferences_module, "save_preferences", fail_save)
    captured: list[str] = []
    io = scripted_io(
        "/settings",
        "n",
        "1",
        "new-leader",
        "1",
        "new-worker",
        "done",
        "4",
        "y",
        "/doctor",
        "/quit",
        echo=captured,
    )

    code = run_shell(
        io,
        statuses=STANDARD_STATUSES,
        settings=settings,
        repository=repository,
        preferences_path=path,
    )

    output = "\n".join(captured)
    assert code == 0
    assert "Could not save preferences" in output
    assert "No changes saved; the active settings are unchanged." in output
    assert "Leader: codex-cli / leader-model" in output
    assert path.read_bytes() == before


def test_wizard_accepts_custom_executable_outside_path(tmp_path: Path) -> None:
    fake = fake_cli(tmp_path / "custom-codex")
    path = tmp_path / "preferences.yaml"
    initial = discover_providers_sync(
        {
            "codex-cli": str(tmp_path / "missing-codex"),
            "claude-code": str(tmp_path / "missing-claude"),
            "opencode": str(tmp_path / "missing-opencode"),
        },
        repository=tmp_path,
    )
    assert not next(item for item in initial if item.provider_id == "codex-cli").installed

    io = scripted_io(
        "y",
        "codex-cli",
        str(fake),
        "n",
        "1",
        "leader-x",
        "1",
        "worker-x",
        "done",
        "2",
        "y",
    )
    built = shell.run_first_run_setup(io, initial, preferences_path=path, repository=tmp_path)

    assert built is not None
    assert built.provider_executables == {"codex-cli": str(fake)}
    assert built.leader == Selection(provider="codex-cli", model="leader-x")
    reloaded = load_preferences(path)
    assert reloaded is not None
    assert reloaded.provider_executables == {"codex-cli": str(fake)}
    assert reloaded.workers == (Selection(provider="codex-cli", model="worker-x"),)


def test_wizard_rejects_invalid_executable_path(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    captured: list[str] = []
    io = scripted_io(
        "y",
        "codex-cli",
        str(tmp_path / "does-not-exist"),
        "",
        "n",
        "1",
        "leader-model",
        "1",
        "worker-model",
        "done",
        "1",
        "y",
        echo=captured,
    )
    built = shell.run_first_run_setup(
        io, STANDARD_STATUSES, preferences_path=path, repository=tmp_path
    )

    output = "\n".join(captured)
    assert built is not None
    assert built.provider_executables == {}
    assert "Not used" in output
    assert built.leader == Selection(provider="codex-cli", model="leader-model")


def test_build_preferences_allows_detection_only_executable_override() -> None:
    built = build_preferences(
        WizardAnswers("codex-cli", "l", (Selection(provider="codex-cli", model="w"),), 1),
        STANDARD_STATUSES,
        executable_overrides={"opencode": "/opt/opencode/bin/opencode"},
    )

    assert built.provider_executables == {"opencode": "/opt/opencode/bin/opencode"}


def test_existing_engineering_commands_remain_available() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for name in (
        "doctor",
        "plan",
        "run",
        "charter",
        "context",
        "leader",
        "architecture",
        "knowledge",
    ):
        assert name in result.stdout
