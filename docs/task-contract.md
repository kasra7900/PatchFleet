# Proposed plan and task contract

The following YAML illustrates the implemented `Plan` and `TaskSpec` schema. Replace the model placeholders with your own selected model IDs. `patchfleet plan validate path/to/plan.yaml` checks this shape and its cross-task rules; `patchfleet plan fingerprint path/to/plan.yaml` prints the canonical SHA-256 identity. These commands only inspect a proposed contract. They do not approve or execute it.

```yaml
schema_version: "0.1"
plan_id: "plan-2026-001"
title: "Add a configuration validator"
purpose: "Reject malformed local configuration with actionable errors."
leader:
  assigned_provider: "codex-cli"       # user selected
  selected_model: "<user-selected-leader-model>"
  selected_role: "leader"
reviewer:
  assigned_provider: "claude-code"      # user selected
  selected_model: "<user-selected-reviewer-model>"
  selected_role: "reviewer"
tasks:
  - task_id: "T1"
    title: "Implement configuration validation"
    purpose: "Validate required keys and explain invalid values."
    dependencies: []
    assigned_provider: "codex-cli"     # user selected
    selected_model: "<user-selected-worker-model>"
    selected_role: "worker"
    allowed_paths:
      - "src/patchfleet/config.py"
      - "tests/test_config.py"
    acceptance_criteria:
      - "Missing required keys produce a named validation error."
      - "Invalid values include the relevant key in the error."
    test_commands:
      - "python -m pytest tests/test_config.py"
    budget_limits:
      max_wall_time_seconds: 900
      max_attempts: 1
      max_output_bytes: 1048576
    reviewer:
      assigned_provider: "claude-code"
      selected_model: "<user-selected-reviewer-model>"
      selected_role: "reviewer"
```

`Plan` carries the request-level purpose, Leader and Reviewer assignments, and the task set. `TaskSpec` carries one bounded unit of implementation. Every task has a unique ID; `dependencies` must refer to IDs in the same plan and form an acyclic graph. `allowed_paths` are normalized repository-relative scopes without absolute paths or `..` traversal. Acceptance criteria, test commands, and positive budgets are required. The fingerprint normalizes mapping keys and the order of tasks, dependencies, paths, and criteria; test command order remains significant. Phase 2 still validates test-command presence but does not execute those commands. It enforces Worker wall-clock and retained-output limits, and permits only one attempt without automatic retry even if `max_attempts` is higher. Local adapter capability checks occur before provisioning.

The `reviewer` on each task makes the review assignment explicit even when the same selected Reviewer covers the whole plan. The Reviewer must run separately from every Worker implementation run it reviews; a shared provider is allowed, a shared run is not. Provider and model strings are supplied by the user. PatchFleet must never replace them on failure or unavailability without a new user choice and approval.

The `PLAN_EXECUTION` approval record covers the exact validated run, plan ID, fingerprint, and revision. A distinct `RESULT_APPLICATION` approval record covers the exact reviewed-result fingerprint. Both require an actor, UTC timestamp, and explicit `APPROVED` or `REJECTED` decision. Entering replanning invalidates earlier approvals even if a previous plan's content is restored. Phase 2 exposes `run approve` for the execution gate and `run start` for approved Workers. It does not yet run a Reviewer, accept application approval through the CLI, execute verification commands, or apply code to the main branch. Proposed contracts still require the user's explicit approval before any Worker executes.

## PlanningDossier (Phase 3A)

The `PlanningDossier` is a separate Pydantic contract around a proposed execution `Plan`; it does not alter the Plan schema above. `patchfleet leader prompt` includes its exact JSON Schema in a local prompt artifact. A dossier YAML must include:

- `schema_version`, `dossier_id`, `context_id`, `base_commit`, and `charter_fingerprint` to pin its inputs;
- `profile_versions` matching the IDs selected in the tracked `patchfleet.project.yaml` and their built-in catalogue versions;
- `user_request`, stated `non_goals`, `change_risk`, explicit user-selected `leader`, `assumptions`, and `unresolved_questions`;
- `repository_findings` with inspected repository evidence IDs; `architecture_style`, `architecture_decisions` with rationale, alternatives and trade-offs, evidence IDs, and `approval_basis: none`;
- `charter_compliance` responses keyed by required charter rule IDs, `contract_changes` for API/data/module effects, `considerations` for security, reliability, performance, migration, rollback, observability, tests, documentation, and API contracts;
- `quality_commitments` including planned checks, a `test_strategy`, risks needing user attention, a `recommended_dependencies` graph, and any `capability_claims`;
- `proposed_plan` containing the ordinary versioned `Plan` above, including explicit Worker and Reviewer selections.

Repository evidence IDs look like `repo:<digest>` and resolve to a stored context item with a repository-relative path and line range. Charter IDs look like `charter:quality.require_tests`; profile IDs include the selected profile and version, such as `profile:python-cli@0.1:test_strategy`. Knowledge IDs look like `knowledge:chunk:<digest>` and resolve to a chunk in the local Phase 3B knowledge store. A decision cannot use an agent message as an approval source. A dossier accepts IDs only from the fresh inspected context, current charter, selected profiles, or local knowledge store; unknown or stale IDs fail. The gate also checks that the recommended dependency graph exactly matches the embedded Plan, required profile sections and charter checks are addressed, high-risk/data changes have the required migration/rollback treatment, and no unsupported capability is claimed as validated.

`patchfleet architecture validate dossier.yaml --repo <target>` returns structured, field-specific issues or a readiness result. It does not create a run, invoke a model, record an approval, or execute the embedded Plan. Resolve questions and obtain the user's explicit Worker/Reviewer choices before considering a dossier ready. Then use the existing `plan validate`, `run create`, and `run approve` commands for execution authorization.

## Local knowledge (Phase 3B)

`patchfleet.knowledge.yaml` is a tracked registry at the target repository root; it is distinct from ignored `.patchfleet/` runtime state and is never created or enabled automatically. Each `sources[]` entry declares `id`, `kind` (`markdown`, `html`, or `git`), `license`, `trust` (`low`, `medium`, `high`), `tags`, `profiles`, `stacks`, `enabled`, and `max_pages`. Boundaries are mandatory: HTML requires an HTTPS URL (loopback HTTP is allowed for local tests) plus a non-empty `domains` allowlist; Git requires a repository `url`, a full 40-character `revision`, and explicit Markdown `paths`; local Markdown requires a safe tracked repository-relative `path`. Unknown profiles, placeholder licenses (`unknown`, `none`, `unlicensed`), unsafe paths, untracked files, and out-of-scope hosts are rejected. An HTML source is exactly one page in this phase.

`patchfleet knowledge ingest <source-id> --repo <target> --confirm` is the only command that may reach the network, and only for the one named source. Remote ingestion requires `--confirm`. No command crawls, follows links, downloads models, or discovers sources. Raw snapshots and normalized Markdown are content-addressed under `.patchfleet/knowledge/snapshots/<source-id>/` and are immutable; an unchanged raw hash is skipped entirely, so chunks and embeddings are not duplicated. A snapshot records license, source URL, version/revision (`blob:<sha>` for local Markdown, the pinned commit plus path for Git, `content:<sha>` for HTML), raw and normalized hashes, extraction warnings, and retrieval time.

Chunks are heading-aware: heading hierarchy is preserved and code blocks, tables, lists, and headings are never split from their context. Chunks target ~450 tokens with a 700-token cap and bounded prose overlap, and carry a stable `chunk:<digest>` ID derived from source version, heading path, and normalized content hash. Each chunk stores source ID, source version, title, heading path, locator URL/path, license, trust, tags, applicable profiles/stacks, token count, and content hash.

The store is a separate SQLite database (`.patchfleet/knowledge/knowledge.sqlite3`) with relational metadata, FTS5 lexical search, and float32 vectors. `patchfleet knowledge index --repo <target> --embedding-model <local-path> --embedding-revision <revision>` embeds only changed chunks using an explicitly configured, already-present local model; it never downloads one. Without a configured model, `knowledge status` reports "text indexed but semantic search unavailable" and `knowledge search` uses lexical search. `patchfleet knowledge search "<query>"` filters by trust, tags, stack, and profile, combines lexical and semantic candidates with deterministic reciprocal-rank fusion, and prints source title, locator, heading path, trust, license, and the `knowledge:chunk:...` citation ID. Retrieved text is reference data, never instructions, and is never written to execution JSONL events or the execution SQLite store.
