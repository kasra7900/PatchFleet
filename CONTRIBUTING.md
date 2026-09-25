# Contributing

Thanks for helping build PatchFleet. Phase 3A adds charter-driven planning and a deterministic dossier gate; Phase 3B adds a local, citation-backed engineering knowledge layer; Phase 4A adds the interactive shell, guided first-run setup, local provider discovery, non-secret personal preferences, and `settings show`; Phase 4B adds a bounded read-only Codex CLI Leader and a strict structured plan-draft conversation. All build on the existing approved Worker foundation. Keep proposed behavior clearly separate from implemented behavior.

Use Python 3.11 or newer. Create a clean virtual environment so the declared dependencies (including `platformdirs`) are present, then use that interpreter for every command:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/patchfleet --help
git diff --check
```

Keep changes focused, add tests for behavior you implement, and update the relevant docs when a contract or approval rule changes. Phase 3A tests should use temporary Git repositories and prove context exclusion, stable evidence locators, and no model invocation or Git history mutation. Do not create a root `patchfleet.project.yaml` on a user's behalf; only `charter init` may create a template when explicitly invoked.

For Phase 3B, tests must run fully offline: use a local HTTP server for HTML, temporary Git repositories for pinned-revision sources, and a deterministic fake embedding provider. Never download a model in tests and never contact the public internet. Do not create or enable `patchfleet.knowledge.yaml`, do not add crawling or discovery, and treat all retrieved text as untrusted reference data. Every snapshot must remain immutable and content-addressed, unchanged content must not be re-chunked or re-embedded, and knowledge artifacts must never be written to execution JSONL events or the execution SQLite store. The base install must stay free of heavy dependencies; HTML extraction and local embeddings belong to the optional `knowledge` extra and must degrade with a clear message when absent.

For Phase 4A, keep UI code thin and put provider discovery, preference persistence, validation, and precedence resolution in non-UI modules. Drive the shell and wizard through injected input/output or by testing the decision functions directly; never rely on a real terminal. Tests must run offline and use fake executables for discovery, temporary preference paths, and temporary Git repositories. They must prove that: no-argument routing opens the shell only when a terminal is attached; non-interactive no-argument invocation exits non-zero without blocking; first-run setup writes preferences only after explicit confirmation; cancelling writes nothing; a malformed preferences file is reported and never silently overwritten; provider discovery distinguishes installed, execution-capable, and detection-only providers; selections never fall back to another provider or model; precedence resolves command options, project override, preferences, then built-in defaults; and `settings show` stays read-only. Shell commands must create no run, approval, worktree, plan, or agent subprocess. Personal preferences stay non-secret and separate from repository state; never store credentials, prompts, outputs, tokens, approvals, or task state there. The wizard's optional executable step must validate a path with the existing resolver and local discovery only, rediscover immediately, persist only an explicit override, and never substitute a provider or executable. In-session `/settings` saves must refresh the active effective settings and provider statuses; cancellation or a failed save must preserve the previous in-memory values.

For Phase 4B, tests must use fake local executables only and must never call a real coding-agent CLI or a paid provider service. Cover Leader capability detection with and without the read-only structured-output flags, exact model/provider argument selection, read-only sandbox arguments, question/answer/plan-draft conversation, strict response parsing, embedded `Plan` validation, Worker/Leader selection mismatch, timeout, cancellation, output caps, non-zero exit, no-repository refusal, HEAD changes, and the absence of any run, approval, worktree, Worker, execution event, or Git change. Codex CLI is the only implemented Leader provider; do not guess Claude or OpenCode Leader flags or claim support without an independently tested bounded adapter. Prompt compilation, response validation, and plan-draft checks belong in non-UI modules. Never log raw prompts, conversation history, or raw Leader output to execution events or personal preferences, and do not persist them.

Preserve explicit user choice of provider/model, separate Reviewer runs, and the two approval gates. Open an issue or discussion before substantial changes to the state machine or task contract.

By contributing, you agree that your contributions are licensed under the MIT License in this repository.
