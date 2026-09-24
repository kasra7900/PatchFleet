"""Small, explicit plan fixtures shared by the control-plane tests."""

import pytest

from patchfleet.contracts import Plan
from patchfleet.validation import validate_plan


@pytest.fixture
def plan_data() -> dict:
    return {
        "schema_version": "0.1",
        "plan_id": "plan-1",
        "title": "Add local configuration checks",
        "purpose": "Reject malformed configuration before use.",
        "leader": {
            "assigned_provider": "codex-cli",
            "selected_model": "leader-model",
            "selected_role": "leader",
        },
        "reviewer": {
            "assigned_provider": "claude-code",
            "selected_model": "reviewer-model",
            "selected_role": "reviewer",
        },
        "tasks": [
            {
                "task_id": "T1",
                "title": "Validate configuration",
                "purpose": "Report missing and invalid settings.",
                "dependencies": [],
                "assigned_provider": "codex-cli",
                "selected_model": "worker-model",
                "selected_role": "worker",
                "allowed_paths": ["src/patchfleet/config.py", "tests/test_config.py"],
                "acceptance_criteria": ["Missing keys produce a named error."],
                "test_commands": ["python -m pytest tests/test_config.py"],
                "budget_limits": {
                    "max_wall_time_seconds": 900,
                    "max_attempts": 1,
                    "max_output_bytes": 1048576,
                },
                "reviewer": {
                    "assigned_provider": "claude-code",
                    "selected_model": "reviewer-model",
                    "selected_role": "reviewer",
                },
            }
        ],
    }


@pytest.fixture
def plan(plan_data: dict) -> Plan:
    return validate_plan(plan_data)
