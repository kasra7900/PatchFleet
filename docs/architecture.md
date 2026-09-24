# Architecture

## Status

This is the intended v0.1 architecture. Phase 1 implements contracts, validation, guarded transitions, SQLite persistence, JSONL events, and read-only plan CLI commands. Modules for agent execution and Git integration below remain design targets.

## Proposed Python modules

| Module | Intended responsibility |
| --- | --- |
| `cli.py` | Implemented: Typer help and read-only YAML plan validation/fingerprinting. Interactive approvals and run status remain future work. |
| `contracts.py` | Implemented: Pydantic `Plan`, `TaskSpec`, assignments, budgets, approvals, run records, and canonical plan fingerprints. |
| `validation.py` | Implemented: structured schema/cross-task errors, role and path checks, dependency-cycle detection. Adapter capability checks remain future work. |
| `state.py` | Implemented: explicit states, one pure transition function, legal-transition rules, and approval guards. |
| `events.py` | Implemented: safe event types, payload allowlists, and canonical JSONL serialization. |
| `scheduler.py` | Deterministic dependency scheduling; no agent decides the next state. |
| `orchestrator.py` | Coordinates validation, approvals, worktree lifecycle, execution, verification, and review. |
| `adapters/base.py` | Small interface for invoking a selected agent CLI and capturing a normalized result. |
| `adapters/codex.py`, `adapters/claude.py`, `adapters/opencode.py` | Provider-specific CLI arguments, capability checks, and output parsing. OpenCode is later work. |
| `processes.py` | Async subprocess launch, timeouts, cancellation, and output capture. |
| `worktrees.py` | Git worktree creation, identity, inspection, and safe cleanup. |
| `verification.py` | Execution and recording of approved test commands. |
| `review.py` | Separate Reviewer invocation and findings. |
| `storage.py` | Implemented: SQLite plans/runs/approvals/events, guarded transition persistence, and JSONL reconciliation. Versioned migrations remain future work. |

The adapter interface will receive an explicit provider, model, role, task context, worktree path, and limits. It may translate those into the selected CLI's arguments and normalize exit status and output. Adapters must report unsupported capabilities or missing CLIs; they must not pick a substitute provider or model. Codex CLI and Claude Code are the initial intended adapters, with OpenCode later.

## Local persistence

Phase 1 stores versioned plans, run state, approval records, and ordered events in SQLite under a configurable directory (default `.patchfleet/`). SQLite is authoritative: each mutation and its event commit in one transaction. A per-run JSONL file mirrors committed events with run ID, increasing sequence number, UTC timestamp, type, and a strict safe-payload allowlist. On startup, the store appends a missing JSONL suffix from SQLite and trims only an incomplete trailing write; a completed line that disagrees with SQLite is reported as corruption. The current store supports one process as writer. Agent prompts, output, credentials, and environment variables are not event payload fields.

## Isolation and execution

Each Worker will receive a separate Git worktree and branch, attached to its task/run identity. This keeps concurrent changes separate and lets PatchFleet inspect each result before integration. A worktree is **isolation for source changes**, not a security sandbox: a subprocess can still access files and services allowed to the local user. Path allowlists are task-contract limits that PatchFleet can validate and inspect, not a substitute for OS-level confinement. The user remains responsible for running trusted agent CLIs or adding a separate sandbox.

Future subprocess management will use `asyncio` to supervise selected CLI processes, stream or cap output, enforce time limits where possible, and record exit codes. It is not part of Phase 1.

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

Phase 1 exposes `state.transition` and the transactional `SQLiteStore.transition_run` wrapper. Approvals are explicit records tied to the run, plan ID, plan revision, and exact plan or reviewed-result fingerprint. A later rejection overrides an earlier approval for the same subject. Entering `REPLAN_REQUIRED` increments the revision and invalidates old approvals; `replace_plan` then returns to `PLANNED` for validation and new approval. The `APPLIED` transition has an approval guard, but Phase 1 performs no Git application or review. Future orchestration must call it only after the actual approved application succeeds.

## Why an explicit scheduler

Task dependencies, retries, limits, and approval gates are control-plane rules. A deterministic scheduler and small state machine make those rules inspectable and testable, including after a crash. Agent outputs are inputs to the workflow, not instructions that can change its guards. An agent framework would add another decision layer without solving the local approval and Git integration requirements, so CrewAI, AutoGen, and LangGraph are out of scope. Redis, Celery, Docker, and web frameworks are likewise unnecessary for the initial local workflow.
