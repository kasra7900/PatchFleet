"""Claude Code print-mode Worker adapter."""

from __future__ import annotations

from .base import AdapterRequest, CLIAdapter, task_prompt


class ClaudeCodeAdapter(CLIAdapter):
    provider_id = "claude-code"
    required_help_tokens = ("--print", "--model", "--permission-mode")

    def build_command(self, request: AdapterRequest) -> tuple[list[str], bytes | None]:
        self._check_request(request)
        command = [
            str(self.executable),
            "--print",
            "--model",
            request.model,
            "--permission-mode",
            "acceptEdits",
            task_prompt(request),
        ]
        return command, None
