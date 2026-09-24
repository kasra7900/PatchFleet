"""Explicit, local Phase 3A commands; no model or execution adapters are imported."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError

from .architecture_gate import ArchitectureGateError, validate_dossier
from .charter import CharterValidationError, init_charter, load_charter
from .context import ContextError, inspect_context, load_context, persist_context
from .knowledge_embed import EmbeddingError, load_local_embedding_provider
from .knowledge_retrieval import hybrid_search
from .knowledge_store import KnowledgeStore, KnowledgeStoreError
from .leader_prompt import LeaderPromptError, compile_prompt, persist_prompt, read_request
from .validation import PlanValidationError
from .worktrees import WorktreeError, inspect_repository

charter_app = typer.Typer(
    help="Manage the explicit version-controlled Engineering Charter.", no_args_is_help=True
)
context_app = typer.Typer(help="Inspect safe tracked repository evidence.", no_args_is_help=True)
leader_app = typer.Typer(
    help="Compile a Leader prompt without invoking a model.", no_args_is_help=True
)
architecture_app = typer.Typer(
    help="Validate a planning dossier deterministically.", no_args_is_help=True
)


def _error(label: str, error: Exception) -> None:
    typer.echo(f"{label}: {error}", err=True)
    raise typer.Exit(code=1) from error


def _knowledge_provider(store: KnowledgeStore):
    """Best-effort local provider; lexical retrieval still works without it."""
    state = store.get_index_state()
    if state is None:
        return None
    try:
        return load_local_embedding_provider(str(state["model_path"]), str(state["revision"]))
    except EmbeddingError:
        return None


@charter_app.command("init")
def charter_init(
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Create a commented charter template only on explicit request; never stage it."""
    try:
        root, _ = inspect_repository(repo)
        path = init_charter(root)
    except (OSError, WorktreeError, FileExistsError) as error:
        _error("Could not initialize charter", error)
    typer.echo(f"Created {path}")
    typer.echo("Edit and track this file with Git before compiling a Leader prompt.")


@context_app.command("inspect")
def context_inspect(
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
    selected_paths: Annotated[
        list[str] | None, typer.Option("--path", help="Explicit relevant tracked file; repeatable.")
    ] = None,
) -> None:
    """Store a bounded context snapshot from tracked, non-secret files only."""
    try:
        context = inspect_context(repo, tuple(selected_paths or ()))
        path = persist_context(context)
    except (OSError, WorktreeError, ContextError) as error:
        _error("Could not inspect context", error)
    typer.echo(f"Context ID: {context.context_id}")
    typer.echo(f"Base commit: {context.base_commit}")
    typer.echo(
        f"Tracked files: {context.tracked_file_count}; evidence items: {len(context.evidence)}"
    )
    typer.echo(f"Local snapshot: {path}")
    if context.truncated:
        typer.echo("Evidence limit reached; select specific relevant files with --path.")


@context_app.command("show")
def context_show(
    context_id: Annotated[str, typer.Argument(help="Previously inspected context ID.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Read a stored context summary without modifying the repository."""
    try:
        context = load_context(repo, context_id)
    except (OSError, WorktreeError, ContextError) as error:
        _error("Could not show context", error)
    typer.echo(f"Context {context.context_id} @ {context.base_commit}")
    typer.echo(f"Tree: {json.dumps(context.tree_summary, sort_keys=True)}")
    for item in context.evidence:
        locator = f"{item.locator.path}:{item.locator.start_line}-{item.locator.end_line}"
        typer.echo(f"{item.evidence_id} {locator} {item.summary}")


@leader_app.command("prompt")
def leader_prompt(
    request_path: Annotated[Path, typer.Argument(help="User-authored Markdown request.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
    provider: Annotated[str, typer.Option("--provider", help="User-selected Leader provider.")],
    model: Annotated[str, typer.Option("--model", help="User-selected Leader model.")],
    selected_paths: Annotated[
        list[str] | None, typer.Option("--path", help="Explicit relevant tracked file; repeatable.")
    ] = None,
    knowledge_query: Annotated[
        str | None,
        typer.Option("--knowledge-query", help="Optional hybrid knowledge search query."),
    ] = None,
    knowledge_limit: Annotated[
        int, typer.Option("--knowledge-limit", help="Maximum knowledge citations to include.")
    ] = 5,
) -> None:
    """Compile and store a prompt; never launch the selected Leader."""
    try:
        root, _ = inspect_repository(repo)
        charter = load_charter(root)
        request = read_request(request_path)
        context = inspect_context(root, tuple(selected_paths or ()))
        citations = ()
        if knowledge_query:
            with KnowledgeStore(root, read_only=True) as store:
                results = hybrid_search(
                    store,
                    knowledge_query,
                    embedding_provider=_knowledge_provider(store),
                    limit=knowledge_limit,
                )
                citations = tuple(result.citation for result in results)
        prompt = compile_prompt(
            request,
            provider=provider,
            model=model,
            charter=charter,
            context=context,
            knowledge=citations,
        )
        persist_context(context)
        path = persist_prompt(context, prompt)
    except (
        OSError,
        WorktreeError,
        CharterValidationError,
        ContextError,
        KnowledgeStoreError,
        LeaderPromptError,
        ValidationError,
    ) as error:
        _error("Could not compile Leader prompt", error)
    typer.echo(f"Local prompt: {path}")
    typer.echo(
        f"Leader: {provider} / {model}; context: {context.context_id}; bytes: {len(prompt.encode('utf-8'))}"
    )
    typer.echo("No model was invoked and no execution approval was recorded.")


@architecture_app.command("validate")
def architecture_validate(
    dossier_path: Annotated[Path, typer.Argument(help="PlanningDossier YAML file.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")],
) -> None:
    """Check dossier readiness without creating a run or approval."""
    try:
        with dossier_path.open("r", encoding="utf-8") as source:
            document = yaml.safe_load(source)
        dossier = validate_dossier(document, repo)
    except ArchitectureGateError as error:
        typer.echo("Planning dossier is not ready:", err=True)
        for item in error.issues:
            typer.echo(f"  {item.path} [{item.code}]: {item.message}", err=True)
        raise typer.Exit(code=1) from error
    except (OSError, UnicodeError, yaml.YAMLError, PlanValidationError) as error:
        _error("Could not read dossier", error)
    typer.echo(f"Ready dossier {dossier.dossier_id} for context {dossier.context_id}")
    typer.echo("The proposed Plan still needs separate validation and explicit execution approval.")
