"""Contract, graph, scope, and fingerprint checks."""

from copy import deepcopy

import pytest

from patchfleet.contracts import Plan, plan_fingerprint
from patchfleet.validation import PlanValidationError, validate_plan


def issue_codes(value: dict) -> set[str]:
    with pytest.raises(PlanValidationError) as raised:
        validate_plan(value)
    return {issue.code for issue in raised.value.issues}


def test_valid_plan_acceptance(plan_data: dict) -> None:
    plan = validate_plan(plan_data)
    assert isinstance(plan, Plan)
    assert plan.tasks[0].selected_model == "worker-model"


def test_missing_dependency(plan_data: dict) -> None:
    plan_data["tasks"][0]["dependencies"] = ["T9"]
    assert "missing_dependency" in issue_codes(plan_data)


def test_self_dependency(plan_data: dict) -> None:
    plan_data["tasks"][0]["dependencies"] = ["T1"]
    assert "self_dependency" in issue_codes(plan_data)


def test_cyclic_dependency(plan_data: dict) -> None:
    second = deepcopy(plan_data["tasks"][0])
    second["task_id"] = "T2"
    second["dependencies"] = ["T1"]
    plan_data["tasks"][0]["dependencies"] = ["T2"]
    plan_data["tasks"].append(second)
    assert "dependency_cycle" in issue_codes(plan_data)


def test_duplicate_task_id(plan_data: dict) -> None:
    plan_data["tasks"].append(deepcopy(plan_data["tasks"][0]))
    assert "duplicate_task_id" in issue_codes(plan_data)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside", "src/../outside", "C:\\work\\file", ".", "src//file"]
)
def test_invalid_path_scope(plan_data: dict, path: str) -> None:
    plan_data["tasks"][0]["allowed_paths"] = [path]
    assert "invalid_path" in issue_codes(plan_data)


@pytest.mark.parametrize("field", ["allowed_paths", "acceptance_criteria", "test_commands"])
def test_required_task_lists_are_nonempty(plan_data: dict, field: str) -> None:
    plan_data["tasks"][0][field] = []
    assert "too_short" in issue_codes(plan_data)


@pytest.mark.parametrize("field", ["leader", "reviewer"])
def test_missing_assignment(plan_data: dict, field: str) -> None:
    del plan_data[field]
    assert "missing" in issue_codes(plan_data)


@pytest.mark.parametrize("assignment", ["leader", "reviewer", "worker", "task_reviewer"])
@pytest.mark.parametrize("field", ["assigned_provider", "selected_model"])
def test_blank_provider_or_model(plan_data: dict, assignment: str, field: str) -> None:
    if assignment == "worker":
        selected = plan_data["tasks"][0]
    elif assignment == "task_reviewer":
        selected = plan_data["tasks"][0]["reviewer"]
    else:
        selected = plan_data[assignment]
    selected[field] = "   "
    assert "value_error" in issue_codes(plan_data)


def test_missing_task_reviewer(plan_data: dict) -> None:
    del plan_data["tasks"][0]["reviewer"]
    assert "missing" in issue_codes(plan_data)


def test_role_mismatch(plan_data: dict) -> None:
    plan_data["leader"]["selected_role"] = "worker"
    plan_data["tasks"][0]["selected_role"] = "leader"
    plan_data["tasks"][0]["reviewer"]["selected_role"] = "worker"
    assert "role_mismatch" in issue_codes(plan_data)


def test_invalid_role_name(plan_data: dict) -> None:
    plan_data["reviewer"]["selected_role"] = "implementer"
    assert "enum" in issue_codes(plan_data)


def test_budget_must_be_positive(plan_data: dict) -> None:
    plan_data["tasks"][0]["budget_limits"]["max_attempts"] = 0
    assert "greater_than" in issue_codes(plan_data)


def test_budget_rejects_boolean_as_a_limit(plan_data: dict) -> None:
    plan_data["tasks"][0]["budget_limits"]["max_attempts"] = True
    assert "int_type" in issue_codes(plan_data)


def test_fingerprint_is_stable_across_mapping_order(plan_data: dict) -> None:
    original = validate_plan(plan_data)
    reordered = validate_plan(dict(reversed(list(plan_data.items()))))
    assert plan_fingerprint(original) == plan_fingerprint(reordered)
    assert len(plan_fingerprint(original)) == 64


def test_fingerprint_ignores_task_and_path_order(plan_data: dict) -> None:
    second = deepcopy(plan_data["tasks"][0])
    second["task_id"] = "T2"
    second["dependencies"] = ["T1"]
    plan_data["tasks"].append(second)
    original = plan_fingerprint(validate_plan(plan_data))

    reordered = deepcopy(plan_data)
    reordered["tasks"].reverse()
    reordered["tasks"][1]["allowed_paths"].reverse()
    assert plan_fingerprint(validate_plan(reordered)) == original


def test_material_change_changes_fingerprint(plan_data: dict) -> None:
    original = plan_fingerprint(validate_plan(plan_data))
    plan_data["tasks"][0]["purpose"] = "Different purpose."
    assert plan_fingerprint(validate_plan(plan_data)) != original
