"""Deterministic precedence for effective, user-visible defaults.

Precedence, highest first:
1. explicit command options;
2. a valid target-repository ``.patchfleet/config.yaml`` override;
3. valid personal preferences;
4. safe built-in defaults.

The repository override remains an advanced per-project capability and is
compatible with the existing Phase 2 ``config.yaml`` contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError, config_path, load_config
from .discovery import DEFAULT_EXECUTABLES, PROVIDER_IDS
from .preferences import Selection, UserPreferences

DEFAULT_MAX_PARALLEL_WORKERS = 1

SOURCE_COMMAND = "command option"
SOURCE_PROJECT = "project override"
SOURCE_USER = "user preferences"
SOURCE_DEFAULT = "built-in default"
SOURCE_NONE = "not configured"


@dataclass(frozen=True)
class EffectiveSettings:
    leader: Selection | None
    leader_source: str
    workers: tuple[Selection, ...]
    workers_source: str
    max_parallel_workers: int
    max_parallel_workers_source: str
    provider_executables: dict[str, str]
    provider_executable_sources: dict[str, str]
    project_config_path: Path | None
    project_config_error: str | None


def resolve_settings(
    repository: Path | None = None,
    preferences: UserPreferences | None = None,
    *,
    command_options: Mapping[str, object] | None = None,
    provider_ids: tuple[str, ...] = PROVIDER_IDS,
) -> EffectiveSettings:
    """Combine every source without ever inventing a provider or model."""
    options = dict(command_options or {})
    project = None
    project_path: Path | None = None
    project_error: str | None = None
    if repository is not None:
        candidate = config_path(repository)
        if candidate.is_file():
            project_path = candidate
            try:
                project = load_config(repository)
            except ConfigError as error:
                project_error = str(error)

    executables: dict[str, str] = {
        provider_id: DEFAULT_EXECUTABLES.get(provider_id, provider_id)
        for provider_id in provider_ids
    }
    executable_sources = {provider_id: SOURCE_DEFAULT for provider_id in provider_ids}
    if preferences is not None:
        for provider_id, executable in preferences.provider_executables.items():
            executables[provider_id] = executable
            executable_sources[provider_id] = SOURCE_USER
    if project is not None:
        for provider_id, provider in project.providers.items():
            executables[provider_id] = provider.executable
            executable_sources[provider_id] = SOURCE_PROJECT
    for provider_id, executable in dict(options.get("provider_executables", {}) or {}).items():
        executables[provider_id] = str(executable)
        executable_sources[provider_id] = SOURCE_COMMAND

    if "max_parallel_workers" in options:
        max_workers = int(options["max_parallel_workers"])  # type: ignore[arg-type]
        max_source = SOURCE_COMMAND
    elif project is not None:
        max_workers = project.execution.max_parallel_workers
        max_source = SOURCE_PROJECT
    elif preferences is not None:
        max_workers = preferences.max_parallel_workers
        max_source = SOURCE_USER
    else:
        max_workers = DEFAULT_MAX_PARALLEL_WORKERS
        max_source = SOURCE_DEFAULT

    command_leader = options.get("leader")
    if isinstance(command_leader, Selection):
        leader: Selection | None = command_leader
        leader_source = SOURCE_COMMAND
    elif preferences is not None:
        leader = preferences.leader
        leader_source = SOURCE_USER
    else:
        leader = None
        leader_source = SOURCE_NONE

    command_workers = options.get("workers")
    if command_workers:
        workers = tuple(command_workers)  # type: ignore[arg-type]
        workers_source = SOURCE_COMMAND
    elif preferences is not None:
        workers = tuple(preferences.workers)
        workers_source = SOURCE_USER
    else:
        workers = ()
        workers_source = SOURCE_NONE

    return EffectiveSettings(
        leader=leader,
        leader_source=leader_source,
        workers=workers,
        workers_source=workers_source,
        max_parallel_workers=max_workers,
        max_parallel_workers_source=max_source,
        provider_executables=executables,
        provider_executable_sources=executable_sources,
        project_config_path=project_path,
        project_config_error=project_error,
    )
