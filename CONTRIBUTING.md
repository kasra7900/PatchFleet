# Contributing

Thanks for helping build PatchFleet. Phase 3A adds charter-driven planning and a deterministic dossier gate; Phase 3B adds a local, citation-backed engineering knowledge layer. Both build on the existing approved Worker foundation. Keep proposed behavior clearly separate from implemented behavior.

Use Python 3.11 or newer. For a local editable install with tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff format --check .
python -m ruff check .
patchfleet --help
git diff --check
```

Keep changes focused, add tests for behavior you implement, and update the relevant docs when a contract or approval rule changes. Phase 3A tests should use temporary Git repositories and prove context exclusion, stable evidence locators, and no model invocation or Git history mutation. Do not create a root `patchfleet.project.yaml` on a user's behalf; only `charter init` may create a template when explicitly invoked.

For Phase 3B, tests must run fully offline: use a local HTTP server for HTML, temporary Git repositories for pinned-revision sources, and a deterministic fake embedding provider. Never download a model in tests and never contact the public internet. Do not create or enable `patchfleet.knowledge.yaml`, do not add crawling or discovery, and treat all retrieved text as untrusted reference data. Every snapshot must remain immutable and content-addressed, unchanged content must not be re-chunked or re-embedded, and knowledge artifacts must never be written to execution JSONL events or the execution SQLite store. The base install must stay free of heavy dependencies; HTML extraction and local embeddings belong to the optional `knowledge` extra and must degrade with a clear message when absent.

Preserve explicit user choice of provider/model, separate Reviewer runs, and the two approval gates. Open an issue or discussion before substantial changes to the state machine or task contract.

By contributing, you agree that your contributions are licensed under the MIT License in this repository.
