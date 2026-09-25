"""Explicit local CLI adapters; importing does not discover or start providers."""

from .base import AdapterCapability, AdapterRequest, AdapterResult, CLIAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

ADAPTERS: dict[str, type[CLIAdapter]] = {
    "codex-cli": CodexAdapter,
    "claude-code": ClaudeCodeAdapter,
}

__all__ = [
    "ADAPTERS",
    "AdapterCapability",
    "AdapterRequest",
    "AdapterResult",
    "CLIAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
]
