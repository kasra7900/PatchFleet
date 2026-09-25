# Product requirements

## Status and scope

This document describes the intended v0.1 product. Phase 3A adds a tracked Engineering Charter, explicitly selected versioned profiles, safe repository context, compiled Leader prompts, and a separate evidence-backed PlanningDossier gate. Phase 3B adds an optional local knowledge layer. Phase 4A changes the default experience: the bare `patchfleet` command opens a friendly interactive shell with guided first-run setup, local provider discovery, and versioned non-secret personal preferences. Phase 4A does not invoke a Leader or Worker model, does not create a plan or approval, and does not convert dossier readiness into approval. Phase 2 Worker execution remains unchanged. Interactive Leader conversations, verification, Reviewer execution, and applying changes remain future work.

PatchFleet coordinates existing coding-agent CLIs on a local repository. The user explicitly chooses the provider and model for every Leader, Worker, and Reviewer assignment. PatchFleet must not infer, silently change, or fall back to a different provider or model. An unavailable selection blocks execution until the user makes a new choice. The ordinary first run requires no YAML, Charter, dossier, or knowledge registry: PatchFleet discovers installed provider CLIs locally and asks the user for explicit defaults.

## Personas

- **Solo maintainer:** Wants to delegate a change while retaining control over task scope, costs, and integration.
- **Team contributor:** Wants a repeatable, inspectable handoff between planning, implementation, verification, and review in a shared repository.
- **Security-conscious operator:** Wants local state and explicit approvals, and understands that agent CLIs still run with the local user's permissions unless separately sandboxed.

## Primary journey

0. On first use, the user runs `patchfleet` with no configuration. PatchFleet discovers installed provider CLIs locally and the user explicitly chooses a default Leader, one or more default Workers, and a maximum parallel Worker count. In Phase 4A the shell captures a request as a draft and does not yet invoke a Leader.
1. The user identifies a local repository, describes a change, and selects a Leader, one or more Workers, and a Reviewer with explicit provider/model identifiers.
2. The Leader talks with the user, resolves ambiguities, and proposes a structured plan with bounded tasks.
3. PatchFleet validates the proposed contract and presents the plan, assignments, budgets, and intended commands for inspection.
4. The user approves the plan. Rejection returns it for revision without starting Workers.
5. PatchFleet provisions one Git worktree per Worker and schedules only tasks whose dependencies are satisfied.
6. Workers execute their assigned tasks. PatchFleet records outputs, status, and limits; failures pause or require replanning.
7. PatchFleet runs declared verification commands and records their results.
8. The selected Reviewer runs independently of the implementer and reviews the work and evidence.
9. PatchFleet presents the reviewed changes. Only a separate, explicit user approval permits application to the main branch.

## Responsibilities

| Role | Responsibility | Boundary |
| --- | --- | --- |
| Leader | Discuss the request and propose an engineering plan and task breakdown. | Cannot approve its own plan or start Workers. |
| Worker | Implement one bounded, approved task and report results. | Cannot expand paths, assignments, or budgets without a revised approved plan. |
| Reviewer | Independently inspect implementation and verification evidence in a separate agent run. | Cannot implement the task in the same run or authorize application. |
| Orchestrator | Validate contracts, enforce transitions and gates, schedule dependencies, invoke selected adapters, persist local evidence, and pause on failure. | Cannot silently choose a provider/model or bypass user approval. |

The Reviewer may use a provider also used by a Worker, but the review must be a distinct run from every implementation run it reviews.

## Functional requirements for v0.1

- **FR-01 — Explicit selection:** Capture the user-selected provider and model for each role and each task. Reject missing or unsupported selections; do not auto-substitute.
- **FR-02 — Structured plan:** Accept a versioned `Plan` containing `TaskSpec` records with dependencies, path scope, acceptance criteria, test commands, budgets, and reviewer assignment.
- **FR-03 — Validation:** Check schema, unique IDs, dependency existence and cycles, required fields, supported adapter capabilities, and approval prerequisites before execution.
- **FR-04 — Approval gates:** Present the plan for approval before Worker execution and the reviewed result for separate approval before applying to the main branch. Record the decision and plan/result identity.
- **FR-05 — Isolation:** Provision a dedicated Git worktree for each Worker and retain a traceable relationship between task, worktree, and changes.
- **FR-06 — Scheduling:** Run only dependency-ready approved tasks; preserve deterministic state transitions and record cancellations, failures, and replans.
- **FR-07 — Verification:** Run declared test commands under explicit limits and retain exit status and output references.
- **FR-08 — Independent review:** Start a separate Reviewer run after implementation and verification; record its findings and disposition.
- **FR-09 — Controlled apply:** Show the candidate changes and apply them to the main branch only after explicit user approval for that result.
- **FR-10 — Local traceability:** Store durable state in SQLite and append events to local JSONL logs so a user can inspect what happened.
- **FR-11 — CLI operation:** Provide the workflow through a CLI without requiring a hosted service or dashboard.
- **FR-12 — Approachable interactive entrypoint:** Open a prompt-based shell by default in a terminal, guide first-run setup, discover provider CLIs locally without network access or agent invocation, and persist only non-secret personal preferences. Never require project YAML, a Charter, a dossier, or a knowledge registry for the ordinary flow; keep them as advanced, opt-in capabilities. Never invent, substitute, or silently fall back to a provider or model.

## Non-functional requirements

- Python 3.11+; a small orchestration core using Typer for the CLI, asyncio and subprocess management for local Worker execution, Pydantic for contracts, SQLite for state, `platformdirs` for user configuration locations, and an explicit state machine.
- Local-first operation without a PatchFleet account, cloud service, GitHub Issues, or web dashboard. Discovery, first-run setup, and `settings show` make no web requests, and PatchFleet sends no telemetry. The selected agent CLIs may have their own authentication and network requirements.
- Deterministic effective-default precedence: explicit command options, then a valid project override, then valid personal preferences, then safe built-in defaults. Personal preferences remain separate from repository state and never contain secrets.
- Crash recovery that can identify interrupted runs and ask the user how to proceed; no hidden automatic apply.
- Clear, inspectable errors and logs; secrets should not be copied into event logs or displayed in routine output.
- Local Git and CLI behavior where supported. The current single-executor lock uses POSIX `fcntl`; Windows support is deferred.
- Tests for contract validation, state transitions, approval gates, and failure handling before v0.1 release.

## Safety and approval requirements

- Approval of a plan authorizes only the listed tasks and limits; any material plan change requires a new validation and approval.
- Approval to execute is distinct from approval to apply. Neither can be inferred from a CLI flag default, an agent message, or a previous run.
- The approved plan and reviewed result must be identifiable when asking for each approval. A changed result invalidates the prior apply approval.
- A Worker must not write to the main branch through PatchFleet's integration path. PatchFleet must not apply code to the main branch without an explicit user decision.
- The Reviewer must have a separate run identity from the implementer. A failed or missing review cannot be treated as approval.
- Path allowlists and worktrees reduce accidental overlap but are not an operating-system security boundary. Untrusted agent CLIs require additional sandboxing outside the v0.1 promise.
- Budget limits should be enforced when measurable; unsupported limits must be reported before execution rather than claimed as enforced.
