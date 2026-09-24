# Contributing

Thanks for helping build PatchFleet. Phase 2 contains typed contracts, guarded local state, and approved local Worker execution; please keep proposed behavior clearly separate from implemented behavior.

Use Python 3.11 or newer. For a local editable install with tests:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff format --check .
python -m ruff check .
patchfleet --help
git diff --check
```

Keep changes focused, add tests for behavior you implement, and update the relevant docs when a contract or approval rule changes. In particular, preserve explicit user choice of provider/model, separate Reviewer runs, and the two approval gates. Open an issue or discussion before substantial changes to the state machine or task contract.

By contributing, you agree that your contributions are licensed under the MIT License in this repository.
