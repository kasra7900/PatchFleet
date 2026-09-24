# Architecture

## Status

This is the intended v0.1 architecture. Phase 2 implements local Worker execution on top of Phase 1 contracts, approval guards, SQLite, and JSONL. Leader planning, verification, review, and application remain design targets.

## Proposed Python modules

| Module | Intended responsibility |
| --- | --- |
| `cli.py` | Implemented: plan validation/fingerprinting, read-only doctor/status, run creation, explicit approval, and Worker start. Interactive planning remains future work. |
| `contracts.py` | Implemented: Pydantic `Plan`, `TaskSpec`, assignments, budgets, approvals, run records, and canonical plan fingerprints. |
| `validation.py` | Implemented: provider-independent structured schema/cross-task errors, role and path checks, dependency-cycle detection. Local adapter capability checks live in `adapters/` and `execution.py`. |
| `state.py` | Implemented: explicit states, one pure transition function, legal-transition rules, and approval guards. |
| `events.py` | Implemented: safe event types, payload allowlists, and canonical JSONL serialization. |
| `config.py` | Implemented: explicit project-local enabled providers, executable locations, and maximum parallel Workers. No providers are enabled by default. |
| `scheduler.py` | Implemented: lexical dependency-ready selection and blocked-dependent detection; no agent decides the next state. |
| `execution.py` | Implemented: approved local Worker orchestration only. Verification and review are later phases. |
| `adapters/base.py` | Implemented: normalized selected-provider/model request, result, and capability checks. |
| `adapters/codex.py`, `adapters/claude_code.py` | Implemented: CLI argument vectors and explicit model selection. OpenCode is later work. |
| `processes.py` | Implemented: shell-free async launch, timeout, cancellation, concurrent output draining, and bounded retention. |
| `worktrees.py` | Implemented: Git worktree creation and changed-path inspection. Worktrees are retained, never automatically cleaned up. |
| `verification.py` | Execution and recording of approved test commands. |
| `review.py` | Separate Reviewer invocation and findings. |
| `storage.py` | Implemented: SQLite plans/runs/approvals/events plus backward-compatible task-run, target, and attempt tables; guarded transitions and JSONL reconciliation. Formal versioned migrations remain future work. |

The adapter interface receives an explicit provider, model, role, task context, worktree path, and limits. It translates these into argv arrays and normalizes exit status and bounded output metadata. `doctor` checks configured executables locally with `--version`/`--help`; a specific model's remote availability cannot be established offline. A CLI rejection fails visibly and never triggers a substitute provider or model. Codex CLI and Claude Code are the initial Worker adapters, with OpenCode later.

## Local persistence

SQLite stores versioned plans, runs, approvals, target commit, task runs, Worker attempts, and ordered events under the target repository's ignored `.patchfleet/` directory. The Python storage API also accepts a different local directory. SQLite is authoritative: each mutation and its event commit in one transaction. A per-run JSONL file mirrors committed events with run ID, increasing sequence number, UTC timestamp, type, and a strict safe-payload allowlist. On startup, the store appends a missing JSONL suffix from SQLite and trims only an incomplete trailing write; a completed line that disagrees with SQLite is reported as corruption. `run status` opens SQLite read-only. The Worker executor holds a local lock; a subsequent start marks previously running attempts interrupted and blocks the run instead of resuming a provider session. Agent prompts, output, credentials, and environment variables are not event payload fields. Output bytes are drained but only bounded in memory; SQLite retains metadata, not raw output.

## Isolation and execution

Each Worker receives a separate Git worktree and branch under the ignored `<target>/.patchfleet/worktrees/` directory, attached to its task/run identity and exact base commit. The primary checkout is never passed as a Worker working directory, and PatchFleet never merges or applies changes in Phase 2. Changed tracked and untracked paths are compared against the approved allowlist after exit; violations fail the task and remain for inspection. A worktree is **isolation for source changes**, not a security sandbox: a subprocess can still access files and services allowed to the local user, including the primary checkout if the CLI itself chooses to reach outside its worktree. The user remains responsible for trusted CLIs or separate OS-level confinement.

Subprocess supervision uses `asyncio.create_subprocess_exec` with argv arrays, concurrent stdout/stderr draining, wall-clock timeout, graceful termination followed by kill, and bounded retained output. It records exit code, duration, timeout/cancellation and truncation metadata. It never uses `shell=True` or automatically retries.

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

Phase 2 enters `VERIFYING` after all Workers succeed and stops there; it does not execute the declared checks. States after `VERIFYING` describe later phases.

`BLOCKED` means external input or an unavailable selected CLI prevents progress. `FAILED` means a run or verification failed. `REPLAN_REQUIRED` means the approved scope or plan must change. `CANCELLED` means the user ended the run. These failure states retain evidence and do not lead directly to `APPLIED`; recovery requires an explicit, validated transition, and material plan changes return to planning and approval. A rejected approval leaves the run waiting, cancelled, or requiring a revised plan according to the user's choice.

Phase 1's `state.transition` and transactional `SQLiteStore.transition_run` remain the run-state authority. Approvals are explicit records tied to the run, plan ID, revision, and exact plan or reviewed-result fingerprint. A later rejection overrides an earlier approval for the same subject. Entering `REPLAN_REQUIRED` increments the revision and invalidates old approvals; `replace_plan` then returns to `PLANNED` for validation and new approval. Phase 2's task-attempt start also checks the exact execution approval. The `APPLIED` transition retains its approval guard, but Phase 2 performs no review or Git application.

## Why an explicit scheduler

Task dependencies, retries, limits, and approval gates are control-plane rules. A deterministic scheduler and small state machine make those rules inspectable and testable, including after a crash. Agent outputs are inputs to the workflow, not instructions that can change its guards. An agent framework would add another decision layer without solving the local approval and Git integration requirements, so CrewAI, AutoGen, and LangGraph are out of scope. Redis, Celery, Docker, and web frameworks are likewise unnecessary for the initial local workflow.
