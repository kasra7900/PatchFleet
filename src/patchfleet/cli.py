"""Local control-plane CLI; execution always requires explicit approval."""

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
import yaml

from .config import ConfigError, load_config
from .contracts import Plan, plan_fingerprint
from .execution import ExecutionError, approve_run, create_run, inspect_capabilities, start_run
from .knowledge_cli import knowledge_app
from .planning_cli import architecture_app, charter_app, context_app, leader_app
from .storage import ApprovalError, SQLiteStore, StoreError
from .validation import PlanValidationError, validate_plan
from .worktrees import WorktreeError, inspect_repository

app = typer.Typer(
    help="Local-first, human-approved coordination for coding-agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
)
plan_app = typer.Typer(
    help="Validate and identify a proposed engineering plan.", no_args_is_help=True
)
app.add_typer(plan_app, name="plan")
run_app = typer.Typer(help="Create, approve, start, and inspect local runs.", no_args_is_help=True)
app.add_typer(run_app, name="run")
app.add_typer(charter_app, name="charter")
app.add_typer(context_app, name="context")
app.add_typer(leader_app, name="leader")
app.add_typer(architecture_app, name="architecture")
app.add_typer(knowledge_app, name="knowledge")


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


@app.command("doctor")
def doctor(
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
    plan_path: Annotated[
        Path | None, typer.Option("--plan", help="Optional plan for selected Worker/model checks.")
    ] = None,
) -> None:
    """Inspect local Git, configuration, and provider CLI capabilities without writes."""
    git = shutil.which("git")
    typer.echo(f"Git: {git or 'unavailable'}")
    if git is None:
        raise typer.Exit(code=1)
    try:
        root, base = inspect_repository(repo)
    except (OSError, WorktreeError) as error:
        typer.echo(f"Repository: unavailable ({error})", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Repository: {root} (HEAD {base})")
    try:
        config = load_config(root)
    except ConfigError as error:
        typer.echo(f"Configuration: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Maximum parallel Workers: {config.execution.max_parallel_workers}")
    if not config.providers:
        typer.echo("Providers: none configured", err=True)
        raise typer.Exit(code=1)
    capabilities = asyncio.run(inspect_capabilities(root, config))
    indexed = {str(row["provider"]): row for row in capabilities}
    for row in capabilities:
        status = "available" if row["available"] else f"unavailable: {row['reason']}"
        typer.echo(
            f"{row['provider']}: {status}; executable={row['executable']}; version={row.get('version') or 'unknown'}; explicit model flag={row.get('model_flag', False)}"
        )
    if plan_path is not None:
        plan = _load_plan(plan_path)
        for task in sorted(plan.tasks, key=lambda item: item.task_id):
            cap = indexed.get(task.assigned_provider)
            supported = bool(cap and cap["available"] and cap.get("model_flag"))
            typer.echo(
                f"Task {task.task_id}: provider={task.assigned_provider} model={task.selected_model}; "
                f"explicit model selection={'supported' if supported else 'unavailable'}; "
                "specific model availability cannot be checked offline"
            )
        if any(not indexed.get(task.assigned_provider, {}).get("available") for task in plan.tasks):
            raise typer.Exit(code=1)
    if not any(row["available"] for row in capabilities):
        raise typer.Exit(code=1)


@run_app.command("create")
def run_create(
    path: Annotated[Path, typer.Argument(help="Validated YAML plan.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Persist an exact plan and target; do not provision or run Workers."""
    plan = _load_plan(path)
    try:
        run_id = create_run(plan, repo)
    except (OSError, WorktreeError, StoreError, sqlite3.Error) as error:
        typer.echo(f"Could not create run: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Run {run_id}: WAITING_FOR_APPROVAL")
    typer.echo(f"Plan fingerprint: {plan_fingerprint(plan)}")


@run_app.command("approve")
def run_approve(
    run_id: Annotated[str, typer.Argument(help="Existing run ID.")],
    actor: Annotated[str, typer.Option("--actor", help="Approving person's name.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Record an explicit exact-plan execution approval."""
    try:
        approve_run(run_id, actor, repo)
    except (
        OSError,
        WorktreeError,
        ExecutionError,
        StoreError,
        ApprovalError,
        sqlite3.Error,
        ValueError,
        KeyError,
    ) as error:
        typer.echo(f"Could not approve run: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Approved PLAN_EXECUTION for {run_id}")


@run_app.command("start")
def run_start(
    run_id: Annotated[str, typer.Argument(help="Approved run ID.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Dispatch dependency-ready Workers in dedicated worktrees only."""
    try:
        state = asyncio.run(start_run(run_id, repo))
    except (OSError, WorktreeError, ExecutionError, StoreError, sqlite3.Error, KeyError) as error:
        typer.echo(f"Could not start run: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Run {run_id}: {state}")
    if state != "VERIFYING":
        raise typer.Exit(code=1)


@run_app.command("status")
def run_status(
    run_id: Annotated[str, typer.Argument(help="Existing run ID.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Read stored run, task, worktree, and attempt metadata without writes."""
    try:
        root, _ = inspect_repository(repo)
        with SQLiteStore(root / ".patchfleet", read_only=True) as store:
            run = store.get_run(run_id)
            target = store.get_target(run_id)
            if target["target_repository"] != str(root):
                raise ExecutionError("run target repository does not match --repo")
            typer.echo(
                f"Run {run.run_id}: {run.state} (plan {run.plan_id}, revision {run.plan_revision})"
            )
            typer.echo(f"Plan fingerprint: {run.plan_fingerprint}")
            typer.echo(f"Target: {target['target_repository']} @ {target['base_commit']}")
            for task in store.list_task_runs(run_id):
                typer.echo(
                    f"{task['task_id']}: {task['state']} provider={task['provider']} model={task['model']}"
                )
                typer.echo(
                    f"  worktree={task['worktree_path'] or '-'} branch={task['branch'] or '-'}"
                )
                if task["reason"]:
                    typer.echo(f"  reason={task['reason']}")
                if task["changed_paths"]:
                    typer.echo(f"  changed={', '.join(task['changed_paths'])}")
                if task["out_of_scope_paths"]:
                    typer.echo(f"  out-of-scope={', '.join(task['out_of_scope_paths'])}")
                for attempt in store.list_attempts(str(task["task_run_id"])):
                    typer.echo(
                        f"  attempt {attempt['attempt_number']}: {attempt['status']} exit={attempt['exit_code']} duration={attempt['duration_seconds']}s truncated={bool(attempt['output_truncated'])}"
                    )
    except (OSError, WorktreeError, ExecutionError, StoreError, sqlite3.Error, KeyError) as error:
        typer.echo(f"Could not inspect run: {error}", err=True)
        raise typer.Exit(code=1) from error
