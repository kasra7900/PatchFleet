# Architecture

## Status

This is the intended v0.1 architecture. Phase 3A adds deterministic Leader planning inputs and a dossier gate without invoking a model. Phase 3B adds a local, citation-backed engineering knowledge layer that can optionally supply reference chunks to the Leader prompt. Phase 2 local Worker execution, Phase 1 contracts, approval guards, SQLite, and JSONL remain intact. Verification, review, and application remain design targets.

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
| `charter.py`, `profiles.py` | Implemented: tracked, user-owned charter contract and four built-in versioned profile requirements. Profiles are never inferred. |
| `context.py` | Implemented: bounded structural evidence from safe tracked files, exact Git HEAD, stable locators, and ignored local context snapshots. |
| `leader_contracts.py` | Implemented: separate typed `PlanningDossier` with decisions, evidence, risks, quality commitments, and a proposed executable `Plan`. |
| `architecture_gate.py` | Implemented: deterministic evidence, charter/profile, risk, dependency, assignment, and existing Plan validation. It records no approval. |
| `leader_prompt.py`, `planning_cli.py` | Implemented: prompt compilation and local planning commands, plus explicit charter template creation; no Leader adapter invocation. The prompt may include optional cited knowledge chunks. |
| `knowledge_registry.py` | Implemented: tracked, license-aware source registry with explicit domain/revision/path boundaries; never inferred or auto-enabled. |
| `knowledge_ingest.py` | Implemented: confirmed ingestion of tracked Markdown, pinned-Git Markdown, and one explicit HTML page; immutable snapshots; optional local-model indexing. |
| `knowledge_chunk.py` | Implemented: heading-aware chunking that keeps code, tables, lists, and headings intact with stable chunk IDs. |
| `knowledge_store.py` | Implemented: separate local SQLite store with relational metadata, FTS5 lexical index, and float32 vectors. Never touches execution events. |
| `knowledge_embed.py` | Implemented: explicit local Sentence Transformers provider and vector/cosine helpers. No automatic downloads. |
| `knowledge_retrieval.py` | Implemented: deterministic hybrid lexical+semantic retrieval via reciprocal-rank fusion with trust/tag/profile/stack filters. |
| `knowledge_cli.py` | Implemented: validate, status, ingest, index, and search commands. |

The adapter interface receives an explicit provider, model, role, task context, worktree path, and limits. It translates these into argv arrays and normalizes exit status and bounded output metadata. `doctor` checks configured executables locally with `--version`/`--help`; a specific model's remote availability cannot be established offline. A CLI rejection fails visibly and never triggers a substitute provider or model. Codex CLI and Claude Code are the initial Worker adapters, with OpenCode later.

## Leader planning boundary (Phase 3A)

`patchfleet.project.yaml` is a tracked file at the target repository root. Its explicit profile IDs resolve to four built-in, versioned data-only profiles: `python-cli`, `python-service`, `security-sensitive`, and `database-change`. The charter can declare stack, quality checks, architecture style and forbidden patterns, security sensitivity, migration/rollback/observability policies, and non-negotiable rules. `charter init` writes a commented template only when invoked; it neither tracks nor commits the file.

`context inspect` reads Git-tracked paths only. It excludes secret-like names, ignored local state, symlinks, binaries, oversized files, and arbitrary untracked files. The snapshot records a bounded tree summary and structural evidence (manifest metadata, test/CI configuration, documentation headings and contribution rules, and public Python symbols) with repository-relative line locators and file digests. A context ID binds this evidence to the target path and exact HEAD. A user can add relevant tracked files via `--path`; PatchFleet does not infer an engineering profile. Context and prompt artifacts live under ignored `.patchfleet/planning/`, separate from SQLite execution events. Prompts are not written to JSONL.

The compiled prompt contains the explicit Leader selection, quoted user request, charter, selected profile requirements, inspected evidence IDs, optional cited knowledge chunks, and the `PlanningDossier` JSON Schema. It is only a local artifact: no Leader CLI is launched. The dossier has its own schema and embeds a proposed Phase 1 `Plan`; it does not modify the executable Plan contract. The Architecture Gate re-inspects the repository, rejects stale or invented evidence, applies charter/profile requirements, validates the embedded Plan and task graph, and rejects unsupported capability claims or agent-message approval claims. A passing gate is a readiness assessment, **not** a run, an approval record, or permission for Workers. The user must still validate, create, and explicitly approve the execution Plan through the existing Phase 1/2 path. Knowledge citations are accepted only when they resolve to a chunk in the local Phase 3B store.

## Local knowledge boundary (Phase 3B)

`patchfleet.knowledge.yaml` is a tracked file at the target repository root, separate from ignored `.patchfleet/` state. Every source must declare a real license, a trust level, a canonical URL or repository URL, and explicit boundaries: allowlisted domains for HTML, a full pinned commit and explicit Markdown paths for Git, or a safe tracked path for local Markdown. PatchFleet never creates, enables, or infers a source. `knowledge validate` checks the registry without ingesting anything.

Ingestion is deliberately narrow: tracked local Markdown, explicit Markdown from a pinned Git revision, or one explicit HTML page per source. No PDFs, recursive crawling, books, or general web search. Remote HTML is HTTPS-only (loopback HTTP is permitted for local testing), redirects are re-checked against the allowlisted domains on every hop, and a timeout and size limit apply with a clear PatchFleet user agent. Raw bytes and normalized Markdown are written once under `.patchfleet/knowledge/snapshots/` with content-addressed names and are never overwritten; re-ingestion of an unchanged content hash is skipped, so no duplication occurs. Snapshots record license, source URL, version/revision, raw and normalized hashes, extraction warnings, and retrieval time.

Normalization then produces heading-aware chunks that preserve heading hierarchy and never split a code block, table, list, or heading from its context. Chunks target ~450 tokens with a 700-token cap and bounded prose overlap, carry a stable ID from source version, heading path, and content hash, and include title, heading path, locator, license, trust, tags, profiles, stacks, token count, and content hash. HTML extraction uses Trafilatura when installed and otherwise a conservative fallback that records a warning.

The knowledge store is a separate SQLite database (`knowledge.sqlite3`) with relational source/snapshot/document/chunk metadata, FTS5 for BM25 lexical retrieval, and float32 vectors for semantic retrieval. Embeddings are optional: `knowledge index` requires an explicit existing local model path and revision and never downloads a model. When no model is configured, `knowledge status` reports "text indexed but semantic search unavailable" and retrieval stays lexical. Hybrid retrieval filters by trust, tags, stack, and engineering profile, retrieves lexical and semantic candidates separately, fuses them deterministically with reciprocal-rank fusion, and returns a small citation-bearing set. Retrieved text is reference data only: it is quoted and framed as non-instructional in the Leader prompt, and it never enters execution JSONL events or the execution SQLite store.

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
