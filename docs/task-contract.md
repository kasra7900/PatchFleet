# Proposed plan and task contract

The following YAML illustrates the implemented Phase 1 `Plan` and `TaskSpec` schema. Replace the model placeholders with your own selected model IDs. `patchfleet plan validate path/to/plan.yaml` checks this shape and its cross-task rules; `patchfleet plan fingerprint path/to/plan.yaml` prints the canonical SHA-256 identity. These commands only inspect a proposed contract. They do not approve or execute it.

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

`Plan` carries the request-level purpose, Leader and Reviewer assignments, and the task set. `TaskSpec` carries one bounded unit of implementation. Every task has a unique ID; `dependencies` must refer to IDs in the same plan and form an acyclic graph. `allowed_paths` are normalized repository-relative scopes without absolute paths or `..` traversal. Acceptance criteria, test commands, and positive budgets are required. The fingerprint normalizes mapping keys and the order of tasks, dependencies, paths, and criteria; test command order remains significant. Phase 1 validates their presence but does not run commands or enforce runtime budgets; adapter capability checks belong to later phases.

The `reviewer` on each task makes the review assignment explicit even when the same selected Reviewer covers the whole plan. The Reviewer must run separately from every Worker implementation run it reviews; a shared provider is allowed, a shared run is not. Provider and model strings are supplied by the user. PatchFleet must never replace them on failure or unavailability without a new user choice and approval.

The `PLAN_EXECUTION` approval record covers the exact validated plan ID, fingerprint, and revision. A distinct `RESULT_APPLICATION` approval record covers the exact reviewed-result fingerprint. Both require an actor, UTC timestamp, and explicit `APPROVED` or `REJECTED` decision. Entering replanning invalidates earlier approvals even if a previous plan's content is restored. Phase 1 enforces these guards in the state machine; it does not yet provide approval CLI commands, run a Reviewer, or apply code to the main branch.
