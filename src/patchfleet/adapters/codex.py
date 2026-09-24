"""Codex CLI non-interactive Worker adapter."""

from __future__ import annotations

from .base import AdapterRequest, CLIAdapter, task_prompt


class CodexAdapter(CLIAdapter):
    provider_id = "codex-cli"
    required_help_tokens = ("--model", "--sandbox")
    help_args = ("exec", "--help")

    def build_command(self, request: AdapterRequest) -> tuple[list[str], bytes]:
        self._check_request(request)
        command = [
            str(self.executable),
            "exec",
            "--model",
            request.model,
            "--sandbox",
            "workspace-write",
            "-",
        ]
        return command, task_prompt(request).encode("utf-8")
