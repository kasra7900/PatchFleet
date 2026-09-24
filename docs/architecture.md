# Architecture

## Status

This is the intended v0.1 architecture. Phase 0 contains only `patchfleet.__init__` and `patchfleet.cli`; the modules below are design targets, not implemented APIs.

## Proposed Python modules

| Module | Intended responsibility |
| --- | --- |
| `cli.py` | Typer entry point, local commands, readable prompts and status. |
| `contracts.py` | Pydantic `Plan`, `TaskSpec`, assignments, budgets, and result schemas. |
| `validation.py` | Schema-level and cross-task checks, adapter capability checks, dependency-cycle detection. |
| `state.py` | Explicit execution states, legal transitions, and approval guards. |
| `scheduler.py` | Deterministic dependency scheduling; no agent decides the next state. |
| `orchestrator.py` | Coordinates validation, approvals, worktree lifecycle, execution, verification, and review. |
| `adapters/base.py` | Small interface for invoking a selected agent CLI and capturing a normalized result. |
| `adapters/codex.py`, `adapters/claude.py`, `adapters/opencode.py` | Provider-specific CLI arguments, capability checks, and output parsing. OpenCode is later work. |
| `processes.py` | Async subprocess launch, timeouts, cancellation, and output capture. |
| `worktrees.py` | Git worktree creation, identity, inspection, and safe cleanup. |
| `verification.py` | Execution and recording of approved test commands. |
| `review.py` | Separate Reviewer invocation and findings. |
| `storage.py` | SQLite repositories, migrations, and append-only JSONL event writer. |

The adapter interface will receive an explicit provider, model, role, task context, worktree path, and limits. It may translate those into the selected CLI's arguments and normalize exit status and output. Adapters must report unsupported capabilities or missing CLIs; they must not pick a substitute provider or model. Codex CLI and Claude Code are the initial intended adapters, with OpenCode later.

## Local persistence

SQLite will store authoritative current state: plans, assignments, task/run identities, approvals, worktree references, and result metadata. An append-only JSONL log will record timestamped transitions and important actions for inspection and audit. Each event should carry a stable run ID, sequence number, event type, and redacted payload. The system should serialize state changes, write durable event records, and reconcile an interrupted write on startup rather than assume a database and a file commit atomically together. Neither store should contain raw credentials.

## Isolation and execution

Each Worker will receive a separate Git worktree and branch, attached to its task/run identity. This keeps concurrent changes separate and lets PatchFleet inspect each result before integration. A worktree is **isolation for source changes**, not a security sandbox: a subprocess can still access files and services allowed to the local user. Path allowlists are task-contract limits that PatchFleet can validate and inspect, not a substitute for OS-level confinement. The user remains responsible for running trusted agent CLIs or adding a separate sandbox.

Future subprocess management will use `asyncio` to supervise selected CLI processes, stream or cap output, enforce time limits where possible, and record exit codes. It will not run in Phase 0.

## State machine

The expected successful path is:

```text
DRAFT → PLANNED → VALIDATED → WAITING_FOR_APPROVAL → PROVISIONING
      → RUNNING → VERIFYING → REVIEWING → WAITING_FOR_APPLY_APPROVAL → APPLIED
```

| State | Meaning and exit condition |
| --- | --- |
| `DRAFT` | User request and explicit role selections are being prepared. |
| `PLANNED` | Leader produced a structured plan. |
| `VALIDATED` | Contract and cross-task checks passed for this plan version. |
| `WAITING_FOR_APPROVAL` | User must approve this exact plan before provisioning or Worker execution. |
| `PROVISIONING` | Approved task worktrees and run records are prepared. |
| `RUNNING` | Dependency-ready Workers execute bounded tasks. |
| `VERIFYING` | Declared checks execute and evidence is collected. |
| `REVIEWING` | A distinct Reviewer run examines implementation and evidence. |
| `WAITING_FOR_APPLY_APPROVAL` | Reviewed result is presented for explicit user approval. |
| `APPLIED` | Approved result was applied to the main branch. |

`BLOCKED` means external input or an unavailable selected CLI prevents progress. `FAILED` means a run or verification failed. `REPLAN_REQUIRED` means the approved scope or plan must change. `CANCELLED` means the user ended the run. These failure states retain evidence and do not lead directly to `APPLIED`; recovery requires an explicit, validated transition, and material plan changes return to planning and approval. A rejected approval leaves the run waiting, cancelled, or requiring a revised plan according to the user's choice.

Transitions are keyed to a versioned plan and run identity. Approval for one version or result cannot authorize another. Only the orchestrator changes state, after checking the transition guard and recording the event.

## Why an explicit scheduler

Task dependencies, retries, limits, and approval gates are control-plane rules. A deterministic scheduler and small state machine make those rules inspectable and testable, including after a crash. Agent outputs are inputs to the workflow, not instructions that can change its guards. An agent framework would add another decision layer without solving the local approval and Git integration requirements, so CrewAI, AutoGen, and LangGraph are out of scope. Redis, Celery, Docker, and web frameworks are likewise unnecessary for the initial local workflow.
