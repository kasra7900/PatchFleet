"""Bounded, read-only Leader planning adapters.

A Leader adapter runs only the explicitly selected local CLI executable, in a
documented non-interactive mode, with the strongest documented read-only
sandbox, an explicit model argument, and bounded output. It never modifies the
repository, starts Workers, or records approvals.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .processes import ProcessResult, supervise


class LeaderUnavailable(ValueError):
    """The exact selected Leader provider/model cannot be invoked safely."""


@dataclass(frozen=True)
class LeaderRequest:
    provider: str
    model: str
    repository: Path
    prompt: str


@dataclass(frozen=True)
class LeaderCapability:
    provider: str
    executable: str
    available: bool
    version: str | None
    structured_output: bool
    reason: str | None


class LeaderAdapter:
    provider_id: str
    required_help_tokens: tuple[str, ...]
    help_args: tuple[str, ...] = ("exec", "--help")

    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def build_command(
        self, request: LeaderRequest, *, schema_path: Path, output_path: Path
    ) -> list[str]:
        raise NotImplementedError

    def read_final_message(self, output_path: Path, stdout: bytes) -> str:
        if output_path.is_file():
            return output_path.read_text(encoding="utf-8")
        return stdout.decode("utf-8", "replace")

    def _check_request(self, request: LeaderRequest) -> None:
        if request.provider != self.provider_id:
            raise LeaderUnavailable("selected provider does not match this Leader adapter")
        if not request.model.strip():
            raise LeaderUnavailable("an explicit selected model is required")
        if request.model.startswith("-") or any(
            character.isspace() or ord(character) < 32 for character in request.model
        ):
            raise LeaderUnavailable("selected model cannot be represented as one safe CLI argument")

    async def detect(self) -> LeaderCapability:
        try:
            version = await supervise(
                [str(self.executable), "--version"], timeout=5, max_output_bytes=4096
            )
            help_result = await supervise(
                [str(self.executable), *self.help_args], timeout=5, max_output_bytes=32768
            )
        except (OSError, ValueError) as error:
            return LeaderCapability(
                self.provider_id, str(self.executable), False, None, False, str(error)
            )
        help_text = (help_result.stdout + help_result.stderr).decode("utf-8", "replace")
        structured = help_result.status == "succeeded" and all(
            token in help_text for token in self.required_help_tokens
        )
        version_text = _text(version) or None
        available = version.status == "succeeded" and structured
        return LeaderCapability(
            self.provider_id,
            str(self.executable),
            available,
            version_text,
            structured,
            None if available else "required read-only structured-output flags unavailable",
        )


def _text(result: ProcessResult) -> str:
    return (result.stdout + result.stderr).decode("utf-8", "replace").strip()


class CodexLeaderAdapter(LeaderAdapter):
    """Codex CLI Leader via ``codex exec`` with read-only sandbox and schema output."""

    provider_id = "codex-cli"
    required_help_tokens = (
        "--model",
        "--sandbox",
        "read-only",
        "--output-schema",
        "--output-last-message",
    )
    help_args = ("exec", "--help")

    def build_command(
        self, request: LeaderRequest, *, schema_path: Path, output_path: Path
    ) -> list[str]:
        self._check_request(request)
        return [
            str(self.executable),
            "exec",
            "--model",
            request.model,
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]


LEADER_ADAPTERS: dict[str, type[LeaderAdapter]] = {
    "codex-cli": CodexLeaderAdapter,
}
