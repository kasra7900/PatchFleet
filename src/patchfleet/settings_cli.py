"""User-facing settings surface: read-only ``show`` plus the guided editor."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import preferences as preferences_module
from .preferences import PreferencesError
from .settings import resolve_settings
from .shell import (
    default_shell_io,
    detect_repository,
    is_interactive,
    run_settings_editor,
    settings_summary_lines,
)

settings_app = typer.Typer(
    help="Review or change your personal PatchFleet defaults.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@settings_app.callback()
def settings_root(ctx: typer.Context) -> None:
    """Open the guided settings editor; it writes only after explicit confirmation."""
    if ctx.invoked_subcommand is not None:
        return
    if not is_interactive():
        typer.echo(
            "The settings editor needs an interactive terminal. "
            "Use 'patchfleet settings show' for a read-only summary.",
            err=True,
        )
        raise typer.Exit(code=2)
    run_settings_editor(default_shell_io(), repository=detect_repository())


@settings_app.command("show")
def settings_show(
    repo: Annotated[
        Path | None, typer.Option("--repo", help="Optional target Git checkout root.")
    ] = None,
) -> None:
    """Print effective, non-secret settings and their source without writing."""
    path = preferences_module.user_preferences_path()
    try:
        stored = preferences_module.load_preferences(path)
        preferences_error = None
    except PreferencesError as error:
        stored = None
        preferences_error = str(error)
    repository = repo if repo is not None else detect_repository()
    settings = resolve_settings(repository, stored)
    for line in settings_summary_lines(
        settings,
        preferences_path=path,
        preferences_error=preferences_error,
        preferences_exist=stored is not None,
    ):
        typer.echo(line)
