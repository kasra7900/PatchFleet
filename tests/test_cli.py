"""Smoke tests for the Phase 0 command-line entry point."""

from typer.testing import CliRunner

from patchfleet.cli import app


def test_help_describes_patchfleet() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Local-first, human-approved coordination" in result.stdout
    assert "orchestration is not available yet" in result.stdout
