"""Command-line entry point for PatchFleet's Phase 0 foundation."""

import typer

app = typer.Typer(
    help="Local-first, human-approved coordination for coding-agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def main() -> None:
    """Local-first, human-approved coordination for coding-agent CLIs.

    Phase 0: orchestration is not available yet.
    """
