"""Local, read-only discovery of installed coding-agent CLIs.

Discovery only ever runs the configured executable's local ``--version`` and
``--help`` checks. It never contacts a provider network service, never invokes
an agent task, and never selects a provider or model on the user's behalf.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .adapters import ADAPTERS
from .processes import ProcessResult, supervise


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    default_executable: str
    execution_adapter: bool
    description: str


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "codex-cli",
        "Codex CLI",
        "codex",
        True,
        "OpenAI Codex CLI; a bounded Worker execution adapter exists.",
    ),
    ProviderSpec(
        "claude-code",
        "Claude Code",
        "claude",
        True,
        "Anthropic Claude Code; a bounded Worker execution adapter exists.",
    ),
    ProviderSpec(
        "opencode",
        "OpenCode",
        "opencode",
        False,
        "Detected for future support only; no execution adapter exists in this phase.",
    ),
)

PROVIDER_IDS: tuple[str, ...] = tuple(spec.provider_id for spec in PROVIDERS)
DEFAULT_EXECUTABLES: dict[str, str] = {
    spec.provider_id: spec.default_executable for spec in PROVIDERS
}


@dataclass(frozen=True)
class ProviderStatus:
    provider_id: str
    display_name: str
    executable: str
    installed: bool
    version: str | None
    execution_adapter: bool
    adapter_ready: bool
    detection_only: bool
    reason: str | None

    @property
    def execution_capable(self) -> bool:
        return self.installed and self.execution_adapter and self.adapter_ready


def resolve_executable(executable: str, base: Path | None = None) -> Path | None:
    """Resolve only the given executable; never substitute a different one."""
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        path = candidate if candidate.is_absolute() else (base or Path.cwd()) / candidate
        path = path.resolve()
    else:
        found = shutil.which(executable)
        if found is None:
            return None
        path = Path(found).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        return None
    return path


def _text(result: ProcessResult) -> str:
    return (result.stdout + result.stderr).decode("utf-8", "replace").strip()


async def discover_providers(
    executables: dict[str, str] | None = None,
    *,
    repository: Path | None = None,
) -> tuple[ProviderStatus, ...]:
    """Probe each known provider CLI locally without contacting any service."""
    configured = dict(DEFAULT_EXECUTABLES)
    if executables:
        configured.update({key: value for key, value in executables.items() if value})
    statuses: list[ProviderStatus] = []
    for spec in PROVIDERS:
        executable = configured.get(spec.provider_id, spec.default_executable)
        resolved = resolve_executable(executable, repository)
        if resolved is None:
            statuses.append(
                ProviderStatus(
                    spec.provider_id,
                    spec.display_name,
                    executable,
                    False,
                    None,
                    spec.execution_adapter,
                    False,
                    not spec.execution_adapter,
                    "configured executable not found or not executable",
                )
            )
            continue
        if spec.execution_adapter:
            capability = await ADAPTERS[spec.provider_id](resolved).detect()
            installed = capability.version is not None or capability.available
            statuses.append(
                ProviderStatus(
                    spec.provider_id,
                    spec.display_name,
                    str(resolved),
                    installed,
                    capability.version,
                    True,
                    capability.available,
                    False,
                    capability.reason,
                )
            )
            continue
        try:
            version_result = await supervise(
                [str(resolved), "--version"], timeout=5, max_output_bytes=4096
            )
            help_result = await supervise(
                [str(resolved), "--help"], timeout=5, max_output_bytes=8192
            )
        except (OSError, ValueError) as error:
            statuses.append(
                ProviderStatus(
                    spec.provider_id,
                    spec.display_name,
                    str(resolved),
                    False,
                    None,
                    False,
                    False,
                    True,
                    str(error),
                )
            )
            continue
        installed = version_result.status == "succeeded" or help_result.status == "succeeded"
        statuses.append(
            ProviderStatus(
                spec.provider_id,
                spec.display_name,
                str(resolved),
                installed,
                _text(version_result) or None,
                False,
                False,
                True,
                None
                if installed
                else "executable responded to no local capability check (--version/--help)",
            )
        )
    return tuple(statuses)


def discover_providers_sync(
    executables: dict[str, str] | None = None,
    *,
    repository: Path | None = None,
) -> tuple[ProviderStatus, ...]:
    """Synchronous wrapper used by the CLI and shell."""
    return asyncio.run(discover_providers(executables, repository=repository))
