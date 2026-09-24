"""Normalized Worker request/result and local CLI capability checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from patchfleet.contracts import BudgetLimits, Role
from patchfleet.processes import ProcessResult, supervise


class AdapterUnavailable(ValueError):
    """The exact selected provider/model cannot be invoked safely."""


@dataclass(frozen=True)
class AdapterRequest:
    run_id: str
    task_id: str
    provider: str
    model: str
    role: Role
    worktree_path: Path
    title: str
    purpose: str
    allowed_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    budget_limits: BudgetLimits


@dataclass(frozen=True)
class AdapterCapability:
    provider: str
    executable: str
    available: bool
    version: str | None
    model_selection_supported: bool
    reason: str | None


@dataclass(frozen=True)
class AdapterResult:
    status: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    pid: int

    @classmethod
    def from_process(cls, result: ProcessResult) -> AdapterResult:
        return cls(
            result.status,
            result.exit_code,
            result.duration_seconds,
            result.timed_out,
            result.cancelled,
            result.stdout_bytes,
            result.stderr_bytes,
            result.output_truncated,
            result.pid,
        )


class CLIAdapter:
    provider_id: str
    required_help_tokens: tuple[str, ...]
    help_args: tuple[str, ...] = ("--help",)

    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def build_command(self, request: AdapterRequest) -> tuple[list[str], bytes | None]:
        raise NotImplementedError

    async def detect(self) -> AdapterCapability:
        try:
            version = await supervise(
                [str(self.executable), "--version"], timeout=5, max_output_bytes=4096
            )
            help_result = await supervise(
                [str(self.executable), *self.help_args], timeout=5, max_output_bytes=32768
            )
        except (OSError, ValueError) as error:
            return AdapterCapability(
                self.provider_id, str(self.executable), False, None, False, str(error)
            )
        help_text = (help_result.stdout + help_result.stderr).decode("utf-8", "replace")
        supported = help_result.status == "succeeded" and all(
            token in help_text for token in self.required_help_tokens
        )
        version_text = (version.stdout + version.stderr).decode("utf-8", "replace").strip()
        available = version.status == "succeeded" and supported
        reason = (
            None
            if available
            else "version or required explicit model/permission CLI flags unavailable"
        )
        return AdapterCapability(
            self.provider_id,
            str(self.executable),
            available,
            version_text or None,
            supported,
            reason,
        )

    def _check_request(self, request: AdapterRequest) -> None:
        if request.provider != self.provider_id or request.role != Role.WORKER:
            raise AdapterUnavailable("selected provider or role does not match this Worker adapter")
        if not request.model.strip():
            raise AdapterUnavailable("an explicit selected model is required")
        if request.model.startswith("-") or any(
            character.isspace() or ord(character) < 32 for character in request.model
        ):
            raise AdapterUnavailable(
                "selected model cannot be represented as one safe CLI argument"
            )


def task_prompt(request: AdapterRequest) -> str:
    """Build a bounded task brief; never store it in an event or database."""
    lines = [
        f"Task: {request.title}",
        f"Purpose: {request.purpose}",
        "Allowed paths (change no others):",
        *(f"- {path}" for path in request.allowed_paths),
        "Acceptance criteria:",
        *(f"- {criterion}" for criterion in request.acceptance_criteria),
        "Work only in the current Git worktree. Do not merge, push, or change the primary checkout.",
    ]
    prompt = "\n".join(lines)
    if len(prompt.encode("utf-8")) > 65536:
        raise AdapterUnavailable("task brief exceeds the 64 KiB local prompt limit")
    return prompt
