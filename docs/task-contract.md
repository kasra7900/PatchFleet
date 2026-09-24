# Proposed plan and task contract

The following YAML illustrates a future, versioned `Plan` with one `TaskSpec`. It is a proposal for discussion, not an executable Phase 0 file or a promise that these exact field names are final. PatchFleet will validate a plan and obtain the user's explicit approval before provisioning or running any Worker. A material revision requires renewed approval.

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

`Plan` carries the request-level purpose, Leader and Reviewer assignments, and the ordered task set. `TaskSpec` carries one bounded unit of implementation. Every task has a unique ID; `dependencies` must refer to IDs in the same plan and form an acyclic graph. `allowed_paths` are repository-relative scopes. Test commands and budget limits are displayed for approval and must be checked against adapter capabilities before execution. A limit PatchFleet cannot measure or enforce must be called out rather than silently ignored.

The `reviewer` on each task makes the review assignment explicit even when the same selected Reviewer covers the whole plan. The Reviewer must run separately from every Worker implementation run it reviews; a shared provider is allowed, a shared run is not. Provider and model strings are supplied by the user. PatchFleet must never replace them on failure or unavailability without a new user choice and approval.

The first approval covers the validated plan, task boundaries, and execution. A second approval covers the specific reviewed result before any code is applied to the main branch. Approvals are tied to plan/result identities; edits after approval invalidate the affected approval.
