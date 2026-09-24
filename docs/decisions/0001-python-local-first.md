# ADR 0001: Python and a local-first control plane

- **Status:** Accepted for the Phase 0 foundation
- **Date:** 2026-09-24

## Context

PatchFleet coordinates user-selected coding-agent CLIs around a local Git repository. It needs a small command-line surface, typed task contracts, predictable approval gates, subprocess supervision, durable local state, and isolated Worker changes. It does not need a hosted control plane for its initial workflow.

## Decision

Use Python 3.11+ as the implementation language. Use Typer for the initial CLI; Pydantic for versioned plan and task contracts; `asyncio` with subprocess management for future agent runs; SQLite for local authoritative state; append-only JSONL for inspectable events; and Git worktrees to give each Worker an independent checkout. Keep orchestration in an explicit scheduler and state machine. Phase 0 installs only what its CLI and tests require; the other components arrive as their phases are implemented.

Python provides a mature standard library for local process and file operations and is accessible to contributors. Typer keeps the CLI small while leaving room for future Rich/Textual output. SQLite avoids a required server. JSONL gives a readable event trail. Worktrees use Git's existing isolation model and keep task changes separate, though they are not security sandboxes.

## Consequences

- Packaging and tests target Python 3.11 or newer.
- The control plane remains usable without a PatchFleet account, cloud service, GitHub Issues, or a web dashboard.
- The selected coding-agent CLIs retain their own installation, authentication, and network needs.
- Process cancellation, crash recovery, SQLite/JSONL consistency, worktree cleanup, and approval identity must be specified and tested as implementation progresses.
- No code is applied to the main branch without a separate explicit user approval.

## Alternatives considered

- **Shell scripts only:** quick to start, but difficult to maintain typed contracts, cross-platform process supervision, and durable state transitions.
- **Node.js or Rust:** viable, but would increase the initial contributor/runtime choices without a current requirement that favors them over Python.
- **CrewAI, AutoGen, or LangGraph:** deferred because orchestration gates and dependency scheduling should be deterministic application rules, not agent decisions.
- **Redis, Celery, or a hosted database:** deferred because a single-machine workflow can use SQLite without operating a service.
- **Docker-based isolation:** deferred to avoid a mandatory container dependency. Git worktrees isolate source changes only; stronger security isolation may be considered separately.
- **Web dashboard or TUI first:** deferred while the CLI and core contracts are established.
