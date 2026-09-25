"""Thin interactive terminal shell and guided first-run setup.

Presentation lives here; provider discovery, preference persistence, and
precedence resolution live in :mod:`patchfleet.discovery`,
:mod:`patchfleet.preferences`, and :mod:`patchfleet.settings`. Prompt handling
is separated from the decisions in :func:`build_preferences` so it can be unit
tested with injected input and output.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import typer

from . import planning
from . import preferences as preferences_module
from .discovery import ProviderStatus, discover_providers_sync, resolve_executable
from .leader_adapters import LEADER_ADAPTERS, LeaderAdapter
from .leader_contracts import LeaderQuestions, LeaderResponse
from .preferences import PreferencesError, Selection, UiPreferences, UserPreferences
from .settings import DEFAULT_MAX_PARALLEL_WORKERS, EffectiveSettings, resolve_settings
from .worktrees import WorktreeError, inspect_repository

PlanningRunner = Callable[[planning.PlanningSession], LeaderResponse]


class WizardError(ValueError):
    """The wizard cannot build valid preferences from the given answers."""


@dataclass(frozen=True)
class ShellIO:
    read_line: Callable[[str], str]
    write: Callable[[str], None]
    write_error: Callable[[str], None]


@dataclass(frozen=True)
class WizardAnswers:
    leader_provider: str
    leader_model: str
    workers: tuple[Selection, ...]
    max_parallel_workers: int


def default_shell_io() -> ShellIO:
    def read_line(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            return "/quit"

    return ShellIO(read_line, typer.echo, lambda message: typer.echo(message, err=True))


def is_interactive() -> bool:
    """Report whether both stdin and stdout are attached to a terminal."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def detect_repository(start: Path | None = None) -> Path | None:
    """Read-only best-effort detection of a target checkout root."""
    try:
        root, _ = inspect_repository(start or Path.cwd())
    except (OSError, WorktreeError):
        return None
    return root


def _confirm(io: ShellIO, question: str, *, default: bool) -> bool:
    answer = io.read_line(question).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _safe_model(model: str) -> bool:
    """Mirror the adapter's rule: one safe, non-blank CLI argument."""
    return (
        bool(model)
        and not model.startswith("-")
        and not any(character.isspace() or ord(character) < 32 for character in model)
    )


def execution_capable(statuses: tuple[ProviderStatus, ...]) -> tuple[ProviderStatus, ...]:
    return tuple(status for status in statuses if status.execution_capable)


def leader_capable(statuses: tuple[ProviderStatus, ...]) -> tuple[ProviderStatus, ...]:
    return tuple(status for status in statuses if status.leader_capable)


def _describe(status: ProviderStatus) -> str:
    if not status.installed:
        return f"{status.display_name} ({status.provider_id}): not found"
    version = f"version {status.version}" if status.version else "version unknown"
    capabilities: list[str] = []
    if status.execution_adapter:
        capabilities.append("worker execution: " + ("yes" if status.adapter_ready else "unusable"))
    if status.leader_adapter:
        capabilities.append("leader planning: " + ("yes" if status.leader_ready else "unusable"))
    if not capabilities:
        return (
            f"{status.display_name} ({status.provider_id}): installed ({version}); "
            "detection only (future support)"
        )
    return f"{status.display_name} ({status.provider_id}): installed ({version}); " + "; ".join(
        capabilities
    )


def provider_lines(statuses: tuple[ProviderStatus, ...]) -> list[str]:
    lines = ["Providers discovered locally:"]
    lines.extend(f"  {_describe(status)}" for status in statuses)
    return lines


def compact_provider_line(statuses: tuple[ProviderStatus, ...]) -> str:
    parts = []
    for status in statuses:
        if not status.installed:
            state = "not found"
        elif not (status.execution_adapter or status.leader_adapter):
            state = "detection only"
        else:
            ready = []
            if status.execution_capable:
                ready.append("worker")
            if status.leader_capable:
                ready.append("leader")
            state = "+".join(ready) if ready else "adapter unusable"
        parts.append(f"{status.provider_id}={state}")
    return "Providers: " + ", ".join(parts)


def _selection_text(selection: Selection | None) -> str:
    if selection is None:
        return "not configured"
    return f"{selection.provider} / {selection.model}"


def banner_lines(
    settings: EffectiveSettings,
    statuses: tuple[ProviderStatus, ...],
    *,
    show_provider_status: bool = True,
) -> list[str]:
    lines = [
        "PatchFleet",
        f"Leader: {_selection_text(settings.leader)}",
    ]
    if settings.workers:
        lines.append(
            "Workers: " + ", ".join(f"{item.provider} / {item.model}" for item in settings.workers)
        )
    else:
        lines.append("Workers: not configured")
    lines.append(f"Maximum parallel Workers: {settings.max_parallel_workers}")
    if show_provider_status:
        lines.extend(provider_lines(statuses))
    else:
        lines.append(compact_provider_line(statuses))
    return lines


def settings_summary_lines(
    settings: EffectiveSettings,
    *,
    preferences_path: Path,
    preferences_error: str | None = None,
    preferences_exist: bool = True,
) -> list[str]:
    """Render only user-relevant, non-secret effective values and their source."""
    lines = ["PatchFleet settings"]
    if preferences_error is not None:
        lines.append(f"Personal preferences: unreadable ({preferences_error})")
    elif preferences_exist:
        lines.append(f"Personal preferences: {preferences_path}")
    else:
        lines.append(f"Personal preferences: not created ({preferences_path})")
    if settings.project_config_error is not None:
        lines.append(f"Project override: unusable ({settings.project_config_error})")
    elif settings.project_config_path is not None:
        lines.append(f"Project override: {settings.project_config_path}")
    else:
        lines.append("Project override: none for the current repository")
    lines.append(f"Leader: {_selection_text(settings.leader)}  [{settings.leader_source}]")
    if settings.workers:
        lines.append(f"Workers:  [{settings.workers_source}]")
        lines.extend(f"  {_selection_text(item)}" for item in settings.workers)
    else:
        lines.append("Workers: not configured")
    lines.append(
        f"Maximum parallel Workers: {settings.max_parallel_workers}"
        f"  [{settings.max_parallel_workers_source}]"
    )
    lines.append("Provider executables:")
    for provider_id in sorted(settings.provider_executables):
        executable = settings.provider_executables[provider_id]
        source = settings.provider_executable_sources.get(provider_id, "")
        lines.append(f"  {provider_id}: {executable}  [{source}]")
    lines.append("No credentials, tokens, prompts, outputs, or approvals are stored here.")
    return lines


def build_preferences(
    answers: WizardAnswers,
    statuses: tuple[ProviderStatus, ...],
    *,
    executable_overrides: dict[str, str] | None = None,
    ui: UiPreferences | None = None,
) -> UserPreferences:
    """Turn explicit answers into validated preferences without any fallback."""
    leaders = {status.provider_id: status for status in statuses if status.leader_capable}
    workers_capable = {
        status.provider_id: status for status in statuses if status.execution_capable
    }
    if not leaders:
        raise WizardError(
            "No Leader-planning-capable provider is installed; preferences were not created."
        )
    if not workers_capable:
        raise WizardError(
            "No Worker-execution-capable provider is installed; preferences were not created."
        )
    if answers.leader_provider not in leaders:
        raise WizardError(
            f"Leader provider {answers.leader_provider!r} is not an installed "
            "Leader-planning-capable provider; PatchFleet will not substitute another provider."
        )
    leader_model = answers.leader_model.strip()
    if not _safe_model(leader_model):
        raise WizardError(
            "A safe, non-blank Leader model is required; PatchFleet does not choose one."
        )
    if not answers.workers:
        raise WizardError("At least one explicit Worker selection is required.")
    for worker in answers.workers:
        if worker.provider not in workers_capable:
            raise WizardError(
                f"Worker provider {worker.provider!r} is not an installed "
                "Worker-execution-capable provider; PatchFleet will not substitute another "
                "provider."
            )
        if not _safe_model(worker.model):
            raise WizardError(
                "Every Worker requires a safe, non-blank explicit model; "
                "PatchFleet does not choose one."
            )
    if answers.max_parallel_workers <= 0:
        raise WizardError("Maximum parallel Workers must be a positive number.")
    known_providers = {status.provider_id for status in statuses}
    unknown = sorted(set(executable_overrides or {}) - known_providers)
    if unknown:
        raise WizardError(f"Executable overrides for unknown providers: {', '.join(unknown)}")
    return UserPreferences(
        schema_version="0.1",
        provider_executables=dict(executable_overrides or {}),
        leader=Selection(provider=answers.leader_provider, model=answers.leader_model.strip()),
        workers=tuple(
            Selection(provider=worker.provider, model=worker.model.strip())
            for worker in answers.workers
        ),
        max_parallel_workers=answers.max_parallel_workers,
        ui=ui or UiPreferences(),
    )


def _print_choices(io: ShellIO, capable: tuple[ProviderStatus, ...]) -> None:
    for index, status in enumerate(capable, start=1):
        io.write(f"  {index}. {status.display_name} ({status.provider_id})")


def _choose_provider(
    io: ShellIO, capable: tuple[ProviderStatus, ...], prompt: str
) -> ProviderStatus | None:
    _print_choices(io, capable)
    while True:
        answer = io.read_line(prompt).strip().lower()
        if answer in {"q", "quit", "/quit", "/exit"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(capable):
            return capable[int(answer) - 1]
        io.write_error(f"Enter a listed number between 1 and {len(capable)}, or q to cancel.")


def _read_model(io: ShellIO, status: ProviderStatus, role: str) -> str:
    while True:
        model = io.read_line(
            f"{role} model for {status.display_name} ({status.provider_id}): "
        ).strip()
        if _safe_model(model):
            return model
        io.write_error(
            "Enter one explicit model name (no spaces and no leading dash); "
            "PatchFleet will not choose one for you."
        )


def _choose_provider_id(io: ShellIO, provider_ids: tuple[str, ...]) -> str | None:
    io.write("Known providers: " + ", ".join(provider_ids))
    while True:
        answer = io.read_line("Provider id (or done to skip): ").strip().lower()
        if answer in {"done", "d", "", "skip", "q", "/quit", "/exit"}:
            return None
        if answer in provider_ids:
            return answer
        io.write_error(f"Enter one of: {', '.join(provider_ids)}, or done.")


def _configure_executables(
    io: ShellIO,
    statuses: tuple[ProviderStatus, ...],
    executable_overrides: dict[str, str] | None,
    *,
    repository: Path | None,
    discover: Callable[..., tuple[ProviderStatus, ...]] | None = None,
) -> tuple[tuple[ProviderStatus, ...], dict[str, str]]:
    """Optionally let the user point PatchFleet at a provider executable.

    Entries are validated with the existing resolver and local discovery only;
    nothing is substituted and only an explicit, validated override is kept.
    """
    discover = discover or discover_providers_sync
    overrides = dict(executable_overrides or {})
    base = {status.provider_id: status.executable for status in statuses}
    for line in provider_lines(statuses):
        io.write(line)
    if not _confirm(io, "Configure a provider executable path? [y/N]: ", default=False):
        return statuses, overrides
    provider_ids = tuple(status.provider_id for status in statuses)
    while True:
        provider_id = _choose_provider_id(io, provider_ids)
        if provider_id is None:
            break
        while True:
            entered = io.read_line(f"Executable for {provider_id} (blank to skip): ").strip()
            if not entered:
                break
            candidate = {**overrides, provider_id: entered}
            refreshed = discover({**base, **candidate}, repository=repository)
            chosen = next((item for item in refreshed if item.provider_id == provider_id), None)
            if chosen is None or not chosen.installed:
                reason = chosen.reason if chosen is not None else "unknown provider"
                io.write_error(
                    f"Not used: {entered!r} is invalid for {provider_id} ({reason}). "
                    "Nothing was changed."
                )
                continue
            overrides = candidate
            base = {item.provider_id: item.executable for item in refreshed}
            statuses = refreshed
            io.write(f"  {_describe(chosen)}")
            if chosen.execution_adapter and not chosen.adapter_ready:
                io.write_error(
                    f"{provider_id} is installed but does not expose the required execution "
                    "flags; it cannot be selected as a Leader or Worker."
                )
            break
        if overrides:
            io.write(
                "Executable overrides: "
                + ", ".join(f"{key}={value}" for key, value in sorted(overrides.items()))
            )
        if not _confirm(io, "Configure another executable? [y/N]: ", default=False):
            break
    return statuses, overrides


def _read_max_workers(io: ShellIO) -> int:
    while True:
        raw = io.read_line(f"Maximum parallel Workers [{DEFAULT_MAX_PARALLEL_WORKERS}]: ").strip()
        if raw == "":
            return DEFAULT_MAX_PARALLEL_WORKERS
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        io.write_error("Enter a positive whole number.")


def collect_setup_answers(
    io: ShellIO, statuses: tuple[ProviderStatus, ...]
) -> WizardAnswers | None:
    leaders = leader_capable(statuses)
    workers_capable = execution_capable(statuses)
    if not leaders or not workers_capable:
        io.write_error("No compatible provider CLI was found for planning and execution.")
        for line in provider_lines(statuses):
            io.write_error(line)
        io.write_error(
            "Install and authenticate Codex CLI for Leader planning, and Codex CLI or Claude "
            "Code for Worker execution (or set an executable override), then run patchfleet "
            "again. Preferences were left uncreated."
        )
        return None
    io.write("Select your default Leader and Workers. PatchFleet never substitutes a choice.")
    leader_provider = _choose_provider(io, leaders, "Leader provider number (q to cancel): ")
    if leader_provider is None:
        return None
    leader_model = _read_model(io, leader_provider, "Leader")
    io.write("Now choose one or more default Workers.")
    _print_choices(io, workers_capable)
    workers: list[Selection] = []
    while True:
        answer = io.read_line("Worker provider number (done to finish): ").strip().lower()
        if answer in {"done", "d", ""}:
            if workers:
                break
            io.write_error("Choose at least one Worker before finishing.")
            continue
        if answer in {"q", "quit", "/quit", "/exit"}:
            return None
        if not (answer.isdigit() and 1 <= int(answer) <= len(workers_capable)):
            io.write_error(
                f"Enter a listed number between 1 and {len(workers_capable)}, or done to finish."
            )
            continue
        worker_provider = workers_capable[int(answer) - 1]
        worker_model = _read_model(io, worker_provider, "Worker")
        workers.append(Selection(provider=worker_provider.provider_id, model=worker_model))
        io.write(f"Added Worker {worker_provider.provider_id} / {worker_model}.")
    return WizardAnswers(
        leader_provider=leader_provider.provider_id,
        leader_model=leader_model,
        workers=tuple(workers),
        max_parallel_workers=_read_max_workers(io),
    )


def run_first_run_setup(
    io: ShellIO,
    statuses: tuple[ProviderStatus, ...],
    *,
    preferences_path: Path | None = None,
    executable_overrides: dict[str, str] | None = None,
    repository: Path | None = None,
) -> UserPreferences | None:
    """Run the guided setup; return ``None`` and write nothing if cancelled."""
    statuses, overrides = _configure_executables(
        io, statuses, executable_overrides, repository=repository
    )
    answers = collect_setup_answers(io, statuses)
    if answers is None:
        return None
    try:
        built = build_preferences(answers, statuses, executable_overrides=overrides)
    except WizardError as error:
        io.write_error(str(error))
        return None
    if not _confirm(io, "Save these personal defaults? [Y/n]: ", default=True):
        io.write("Nothing was saved. PatchFleet created no preferences.")
        return None
    try:
        path = preferences_module.save_preferences(built, preferences_path)
    except OSError as error:
        io.write_error(f"Could not save preferences: {error}. Nothing was changed.")
        return None
    io.write(f"Saved personal preferences: {path}")
    return built


def run_settings_editor(
    io: ShellIO,
    *,
    statuses: tuple[ProviderStatus, ...] | None = None,
    repository: Path | None = None,
    preferences_path: Path | None = None,
    executable_overrides: dict[str, str] | None = None,
) -> UserPreferences | None:
    stored, error = _load_preferences(preferences_path)
    persisted = {} if (error is not None or stored is None) else dict(stored.provider_executables)
    if executable_overrides is None:
        executable_overrides = persisted
    if statuses is None:
        settings = resolve_settings(repository, stored if error is None else None)
        statuses = discover_providers_sync(
            {**settings.provider_executables, **executable_overrides}, repository=repository
        )
    io.write("PatchFleet settings editor. Changes are saved only when you confirm.")
    return run_first_run_setup(
        io,
        statuses,
        preferences_path=preferences_path,
        executable_overrides=executable_overrides,
        repository=repository,
    )


def _planning_refusal(
    settings: EffectiveSettings,
    statuses: tuple[ProviderStatus, ...],
    repository: Path | None,
) -> str | None:
    """Explain why planning cannot start, or return ``None`` when it can."""
    if repository is None:
        return (
            "Planning needs a target Git repository. Start PatchFleet inside the repository "
            "you want to plan for."
        )
    if settings.leader is None:
        return "No default Leader is configured. Run /settings to choose one."
    status = next((item for item in statuses if item.provider_id == settings.leader.provider), None)
    if status is None or not status.installed:
        return (
            f"Selected Leader {_selection_text(settings.leader)} is not installed. "
            "Run /settings to choose an installed Leader."
        )
    if not status.leader_capable:
        return (
            f"Selected Leader {_selection_text(settings.leader)} is not Leader-planning capable. "
            "Run /settings to choose a Leader-capable provider."
        )
    return None


def _leader_adapter(
    session: planning.PlanningSession, settings: EffectiveSettings
) -> LeaderAdapter:
    provider = session.leader.provider
    adapter_class = LEADER_ADAPTERS.get(provider)
    if adapter_class is None:
        raise planning.PlanningError(
            "leader_not_capable", f"{provider} has no Leader planning adapter"
        )
    configured = settings.provider_executables.get(provider, provider)
    executable = resolve_executable(configured, base=session.repository)
    if executable is None:
        raise planning.PlanningError(
            "leader_unavailable", f"executable for {provider} was not found"
        )
    return adapter_class(executable)


def _run_leader_turn(
    io: ShellIO,
    session: planning.PlanningSession,
    settings: EffectiveSettings,
    leader_runner: PlanningRunner | None,
) -> None:
    io.write(f"Planning with {_selection_text(settings.leader)}…")
    try:
        if leader_runner is not None:
            response = leader_runner(session)
        else:
            response = planning.invoke_leader_sync(session, _leader_adapter(session, settings))
    except planning.PlanningError as error:
        io.write_error(f"Leader planning failed: {error}.")
        io.write("Nothing changed; revise your request or /cancel.")
        return
    try:
        planning.apply_response(session, response)
    except planning.PlanningError as error:
        io.write_error(f"Leader response rejected: {error}.")
        io.write("Nothing changed; revise your request or /cancel.")
        session.pending_questions = ()
        return
    if isinstance(response, LeaderQuestions):
        io.write("Leader:")
        io.write(f"  {response.message}")
        for question in response.questions:
            io.write(f"  - {question}")
        io.write("Answer below; /cancel discards this conversation.")
    else:
        _render_plan_draft(io, session)


def _render_plan_draft(io: ShellIO, session: planning.PlanningSession) -> None:
    plan = session.plan_draft
    if plan is None:
        io.write("No validated plan draft yet.")
        return
    io.write("Leader proposal:")
    if session.last_message:
        io.write(f"  {session.last_message}")
    io.write("Tasks:")
    for task in plan.tasks:
        dependency_text = f" (after {', '.join(task.dependencies)})" if task.dependencies else ""
        io.write(f"  - {task.task_id}: {task.title}{dependency_text}")
        io.write(f"      {task.assigned_provider} / {task.selected_model}")
    if session.draft_assumptions:
        io.write("Assumptions:")
        for item in session.draft_assumptions:
            io.write(f"  - {item}")
    if session.draft_risks:
        io.write("Risks:")
        for item in session.draft_risks:
            io.write(f"  - {item}")
    if session.draft_worker_usage:
        io.write(f"Worker usage: {session.draft_worker_usage}")
    io.write(f"Maximum parallel Workers: {session.max_parallel_workers}")
    io.write("Planning draft is ready.")
    io.write("No Worker was started and no execution approval exists.")
    io.write("Interactive execution handoff arrives in the next phase.")


def _start_planning(
    io: ShellIO,
    settings: EffectiveSettings,
    statuses: tuple[ProviderStatus, ...],
    repository: Path | None,
    request: str,
    leader_runner: PlanningRunner | None,
) -> planning.PlanningSession | None:
    refusal = _planning_refusal(settings, statuses, repository)
    if refusal is not None:
        io.write_error(refusal)
        return None
    assert settings.leader is not None and repository is not None
    try:
        session = planning.new_session(
            leader=settings.leader,
            workers=settings.workers,
            max_parallel_workers=settings.max_parallel_workers,
            repository=repository,
            request=request,
        )
    except planning.PlanningError as error:
        io.write_error(f"Could not start planning: {error}.")
        return None
    _run_leader_turn(io, session, settings, leader_runner)
    return session


def _print_help(io: ShellIO) -> None:
    io.write("Commands:")
    io.write("  /new [request]  Start or replace a planning conversation.")
    io.write("  /plan           Show the current validated plan draft.")
    io.write("  /cancel         Discard the in-memory conversation.")
    io.write("  /settings       Review or change your personal defaults.")
    io.write("  /doctor         Show local Git, provider, and effective-settings status.")
    io.write("  /help           Show this command list.")
    io.write("  /quit           Leave PatchFleet.")
    io.write("Free text starts planning, or answers the Leader's latest question.")


def _print_doctor(
    io: ShellIO,
    settings: EffectiveSettings,
    statuses: tuple[ProviderStatus, ...],
    repository: Path | None,
) -> None:
    io.write(f"Repository: {repository if repository is not None else 'not detected'}")
    io.write(f"Leader: {_selection_text(settings.leader)}  [{settings.leader_source}]")
    io.write(
        f"Maximum parallel Workers: {settings.max_parallel_workers}"
        f"  [{settings.max_parallel_workers_source}]"
    )
    for line in provider_lines(statuses):
        io.write(line)
    io.write("Read-only check: no Worker, model, run, or approval was started.")


def run_shell(
    io: ShellIO,
    *,
    statuses: tuple[ProviderStatus, ...],
    settings: EffectiveSettings,
    repository: Path | None = None,
    preferences_path: Path | None = None,
    executable_overrides: dict[str, str] | None = None,
    show_provider_status: bool = True,
    leader_runner: PlanningRunner | None = None,
) -> int:
    session: planning.PlanningSession | None = None
    for line in banner_lines(settings, statuses, show_provider_status=show_provider_status):
        io.write(line)
    io.write("Type /help for commands, or describe what you want to build.")
    while True:
        try:
            line = io.read_line("> ")
        except (EOFError, KeyboardInterrupt):
            io.write("")
            io.write("Goodbye.")
            return 0
        line = line.strip()
        if not line:
            continue
        if not line.startswith("/"):
            if session is None or (not session.pending_questions and session.plan_draft is None):
                session = _start_planning(io, settings, statuses, repository, line, leader_runner)
            elif session.pending_questions:
                question = session.pending_questions[0]
                session.dialogue.append((question, line))
                session.pending_questions = session.pending_questions[1:]
                if not session.pending_questions:
                    _run_leader_turn(io, session, settings, leader_runner)
            elif _confirm(
                io, "A plan draft is active. Start a new conversation? [y/N]: ", default=False
            ):
                session = _start_planning(io, settings, statuses, repository, line, leader_runner)
            else:
                io.write(
                    "Keeping the current draft. Use /plan to review it or /cancel to discard it."
                )
            continue
        command, _, rest = line.partition(" ")
        command = command.lower()
        if command in {"/quit", "/exit"}:
            io.write("Goodbye.")
            return 0
        if command == "/help":
            _print_help(io)
        elif command == "/new":
            if session is not None and not _confirm(
                io, "Replace the active planning conversation? [y/N]: ", default=False
            ):
                io.write("Keeping the current conversation.")
                continue
            request = rest.strip() or io.read_line("Describe the task you want to plan: ").strip()
            session = _start_planning(io, settings, statuses, repository, request, leader_runner)
        elif command == "/plan":
            if session is not None and session.plan_draft is not None:
                _render_plan_draft(io, session)
            else:
                io.write("No validated plan draft yet.")
        elif command == "/cancel":
            if session is None:
                io.write("No planning conversation is active.")
            else:
                session = None
                io.write("Conversation discarded. No further Leader request will be sent.")
        elif command == "/doctor":
            _print_doctor(io, settings, statuses, repository)
        elif command == "/settings":
            updated = run_settings_editor(
                io,
                statuses=statuses,
                repository=repository,
                preferences_path=preferences_path,
                executable_overrides=executable_overrides,
            )
            if updated is None:
                io.write("No changes saved; the active settings are unchanged.")
                continue
            stored, error = _load_preferences(preferences_path)
            if error is not None or stored is None:
                io.write_error(
                    f"Saved settings could not be reloaded: {error or 'missing file'}; "
                    "the active settings are unchanged."
                )
                continue
            settings = resolve_settings(repository, stored)
            statuses = discover_providers_sync(settings.provider_executables, repository=repository)
            if session is not None:
                session = None
                io.write("Active settings changed; the in-memory conversation was discarded.")
            io.write("Active defaults updated for this session:")
            io.write(f"Leader: {_selection_text(settings.leader)}")
            if settings.workers:
                io.write(
                    "Workers: "
                    + ", ".join(f"{item.provider} / {item.model}" for item in settings.workers)
                )
            else:
                io.write("Workers: not configured")
            io.write(f"Maximum parallel Workers: {settings.max_parallel_workers}")
        else:
            io.write_error(f"Unknown command: {command}. Type /help for available commands.")


def _load_preferences(
    preferences_path: Path | None,
) -> tuple[UserPreferences | None, str | None]:
    try:
        return preferences_module.load_preferences(preferences_path), None
    except PreferencesError as error:
        return None, str(error)


def run_interactive(
    repository: Path | None = None,
    io: ShellIO | None = None,
    *,
    preferences_path: Path | None = None,
    detect: bool = True,
    leader_runner: PlanningRunner | None = None,
) -> int:
    """Open the first-run setup when needed, then the interactive shell."""
    io = io or default_shell_io()
    repo = detect_repository() if (detect and repository is None) else repository
    prefs, prefs_error = _load_preferences(preferences_path)
    if prefs_error is not None:
        io.write_error(f"PatchFleet could not read your personal preferences: {prefs_error}")
        if _confirm(io, "Reconfigure personal preferences now? [y/N]: ", default=False):
            settings = resolve_settings(repo, None)
            statuses = discover_providers_sync(settings.provider_executables, repository=repo)
            prefs = run_first_run_setup(
                io, statuses, preferences_path=preferences_path, repository=repo
            )
        else:
            io.write(
                "Leaving the existing preferences file untouched. "
                "Run 'patchfleet settings' when ready."
            )
            return 1
        if prefs is None:
            return 0
    if prefs is None:
        io.write("Welcome to PatchFleet. A short setup creates your personal defaults.")
        settings = resolve_settings(repo, None)
        statuses = discover_providers_sync(settings.provider_executables, repository=repo)
        prefs = run_first_run_setup(
            io, statuses, preferences_path=preferences_path, repository=repo
        )
        if prefs is None:
            io.write("Setup cancelled. PatchFleet created no preferences.")
            return 0
    settings = resolve_settings(repo, prefs)
    statuses = discover_providers_sync(settings.provider_executables, repository=repo)
    return run_shell(
        io,
        statuses=statuses,
        settings=settings,
        repository=repo,
        preferences_path=preferences_path,
        show_provider_status=prefs.ui.show_provider_status,
        leader_runner=leader_runner,
    )
