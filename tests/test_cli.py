"""Read-only CLI behavior for help, validation, and plan identity."""

import yaml
from typer.testing import CliRunner

from patchfleet.cli import app
from patchfleet.contracts import plan_fingerprint
from patchfleet.validation import validate_plan


def test_help_describes_patchfleet() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local-first, human-approved coordination" in result.stdout
    assert "plan" in result.stdout


def test_plan_validate_and_fingerprint_commands(tmp_path, plan_data: dict) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    runner = CliRunner()

    validation = runner.invoke(app, ["plan", "validate", str(path)])
    fingerprint = runner.invoke(app, ["plan", "fingerprint", str(path)])

    assert validation.exit_code == 0
    assert "Valid plan plan-1 (1 task(s))" in validation.stdout
    assert fingerprint.exit_code == 0
    assert fingerprint.stdout.strip() == plan_fingerprint(validate_plan(plan_data))


def test_plan_validate_reports_actionable_errors(tmp_path, plan_data: dict) -> None:
    plan_data["tasks"][0]["dependencies"] = ["missing-task"]
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")

    result = CliRunner().invoke(app, ["plan", "validate", str(path)])

    assert result.exit_code != 0
    assert "tasks[0].dependencies" in result.stderr
    assert "missing_dependency" in result.stderr
    assert "missing-task" in result.stderr


def test_plan_fingerprint_rejects_invalid_yaml(tmp_path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("tasks: [not closed", encoding="utf-8")

    result = CliRunner().invoke(app, ["plan", "fingerprint", str(path)])

    assert result.exit_code != 0
    assert "Could not read YAML plan" in result.stderr
