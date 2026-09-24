"""Read-only Phase 1 commands for inspecting structured plans."""

from pathlib import Path
from typing import Annotated

import typer
import yaml

from .contracts import Plan, plan_fingerprint
from .validation import PlanValidationError, validate_plan

app = typer.Typer(
    help="Local-first, human-approved coordination for coding-agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
plan_app = typer.Typer(
    help="Validate and identify a proposed engineering plan.", no_args_is_help=True
)
app.add_typer(plan_app, name="plan")


def _load_plan(path: Path) -> Plan:
    try:
        with path.open("r", encoding="utf-8") as source:
            document = yaml.safe_load(source)
        return validate_plan(document)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        typer.echo(f"Could not read YAML plan: {error}", err=True)
        raise typer.Exit(code=1) from error
    except PlanValidationError as error:
        typer.echo("Invalid plan:", err=True)
        for issue in error.issues:
            typer.echo(f"  {issue.path} [{issue.code}]: {issue.message}", err=True)
        raise typer.Exit(code=1) from error


@plan_app.command("validate")
def validate(path: Annotated[Path, typer.Argument(help="Path to a proposed YAML plan.")]) -> None:
    """Check a plan without executing or approving anything."""
    plan = _load_plan(path)
    typer.echo(f"Valid plan {plan.plan_id} ({len(plan.tasks)} task(s))")
    typer.echo(f"Fingerprint: {plan_fingerprint(plan)}")


@plan_app.command("fingerprint")
def fingerprint(
    path: Annotated[Path, typer.Argument(help="Path to a proposed YAML plan.")],
) -> None:
    """Print the SHA-256 fingerprint of a valid plan."""
    typer.echo(plan_fingerprint(_load_plan(path)))
