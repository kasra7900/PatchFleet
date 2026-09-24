"""Explicit, local Phase 3B knowledge commands; no crawling or model downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .knowledge_contracts import KnowledgeStatus, SourceStatus
from .knowledge_embed import EmbeddingError, load_local_embedding_provider
from .knowledge_ingest import (
    IngestResult,
    KnowledgeIngestError,
    index_embeddings,
    ingest_source,
)
from .knowledge_registry import (
    REGISTRY_NAME,
    KnowledgeRegistry,
    KnowledgeRegistryError,
    load_registry,
)
from .knowledge_retrieval import (
    RetrievalFilters,
    hybrid_search,
)
from .knowledge_store import KnowledgeStore, KnowledgeStoreError
from .worktrees import WorktreeError, inspect_repository

knowledge_app = typer.Typer(
    help="Ingest, index, and search local engineering knowledge with citations.",
    no_args_is_help=True,
)


def _error(label: str, error: Exception) -> None:
    typer.echo(f"{label}: {error}", err=True)
    raise typer.Exit(code=1) from error


def _load_registry_or_none(root: Path) -> KnowledgeRegistry | None:
    try:
        return load_registry(root)
    except KnowledgeRegistryError:
        return None


def _embedding_state(store: KnowledgeStore) -> dict[str, str | int] | None:
    try:
        return store.get_index_state()
    except Exception:  # noqa: BLE001 - a read failure means semantic search is unavailable
        return None


@knowledge_app.command("validate")
def knowledge_validate(
    registry: Annotated[
        str, typer.Argument(help="Registry path (defaults to patchfleet.knowledge.yaml).")
    ] = REGISTRY_NAME,
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")] = Path("."),
) -> None:
    """Validate the tracked source registry; never ingests anything."""
    try:
        root, _ = inspect_repository(repo)
        loaded = load_registry(root, registry)
    except (OSError, WorktreeError, KnowledgeRegistryError) as error:
        _error("Invalid knowledge registry", error)
    enabled = sum(1 for source in loaded.sources if source.enabled)
    typer.echo(f"Valid registry {registry}: {len(loaded.sources)} source(s), {enabled} enabled")
    for source in loaded.sources:
        state = "enabled" if source.enabled else "disabled"
        typer.echo(f"  {source.id}: kind={source.kind} trust={source.trust} {state}")


@knowledge_app.command("status")
def knowledge_status(
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")] = Path("."),
) -> None:
    """Report local knowledge, registry, and semantic availability without writes."""
    try:
        root, _ = inspect_repository(repo)
    except (OSError, WorktreeError) as error:
        _error("Could not inspect repository", error)
    registry = _load_registry_or_none(root)
    registry_present = registry is not None
    source_count = len(registry.sources) if registry is not None else 0
    enabled_count = sum(1 for source in registry.sources if source.enabled) if registry else 0
    status = KnowledgeStatus(
        registry_present=registry_present,
        source_count=source_count,
        enabled_source_count=enabled_count,
        snapshot_count=0,
        chunk_count=0,
        embedded_chunk_count=0,
        embedding_model=None,
        embedding_revision=None,
        semantic_available=False,
        sources=(),
    )
    try:
        with KnowledgeStore(root, read_only=True) as store:
            stats = store.stats()
            state = _embedding_state(store)
            sources = tuple(
                SourceStatus(
                    source_id=str(row["source_id"]),
                    kind=str(row["kind"]),
                    enabled=bool(row["enabled"]),
                    trust=str(row["trust"]),
                    license=str(row["license"]),
                    snapshot_count=int(row["snapshot_count"]),
                    chunk_count=int(row["chunk_count"]),
                    embedded_chunk_count=int(row["embedded_chunk_count"]),
                    latest_retrieved_at=row["latest_retrieved_at"],
                    latest_source_version=row["latest_source_version"],
                )
                for row in store.source_rows()
            )
            status = KnowledgeStatus(
                registry_present=registry_present,
                source_count=source_count,
                enabled_source_count=enabled_count,
                snapshot_count=stats["snapshots"],
                chunk_count=stats["chunks"],
                embedded_chunk_count=stats["embeddings"],
                embedding_model=str(state["model_id"]) if state else None,
                embedding_revision=str(state["revision"]) if state else None,
                semantic_available=bool(
                    state and stats["embeddings"] > 0 and Path(str(state["model_path"])).is_dir()
                ),
                sources=sources,
            )
    except KnowledgeStoreError:
        pass
    typer.echo(
        f"Registry: {'present' if status.registry_present else 'absent'} "
        f"({status.enabled_source_count}/{status.source_count} enabled)"
    )
    typer.echo(
        f"Snapshots: {status.snapshot_count}; chunks: {status.chunk_count}; "
        f"embedded chunks: {status.embedded_chunk_count}"
    )
    typer.echo(f"Semantic: {status.semantic_message}")
    if status.embedding_model:
        typer.echo(f"Embedding model: {status.embedding_model} @ {status.embedding_revision}")
    for source in status.sources:
        typer.echo(
            f"  {source.source_id}: kind={source.kind} trust={source.trust} "
            f"snapshots={source.snapshot_count} chunks={source.chunk_count} "
            f"embedded={source.embedded_chunk_count} license={source.license}"
        )


def _print_ingest(results: tuple[IngestResult, ...]) -> None:
    for result in results:
        state = "unchanged (skipped)" if result.skipped else "ingested"
        typer.echo(
            f"{result.source_id}: {state}; version={result.source_version}; "
            f"chunks={result.chunk_count}; raw={result.raw_sha256[:16]}; "
            f"normalized={result.normalized_sha256[:16]}"
        )
        typer.echo(f"  title: {result.title}")
        typer.echo(f"  locator: {result.source_url}")
        typer.echo(f"  retrieved: {result.retrieved_at}")
        for warning in result.warnings:
            typer.echo(f"  warning: {warning}")


@knowledge_app.command("ingest")
def knowledge_ingest(
    source_id: Annotated[str, typer.Argument(help="Enabled source ID from the registry.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")] = Path("."),
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Confirm explicit ingestion of this source.")
    ] = False,
) -> None:
    """Ingest one explicit source; network sources require --confirm."""
    try:
        root, _ = inspect_repository(repo)
        results = ingest_source(root, source_id, confirm=confirm)
    except (OSError, WorktreeError, KnowledgeRegistryError, KnowledgeIngestError) as error:
        _error("Could not ingest source", error)
    _print_ingest(results)
    if any(result.skipped for result in results):
        typer.echo("Unchanged content was not re-chunked or re-embedded.")


@knowledge_app.command("index")
def knowledge_index(
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")] = Path("."),
    embedding_model: Annotated[
        Path, typer.Option("--embedding-model", help="Path to an existing local model directory.")
    ] = Path(),
    embedding_revision: Annotated[
        str, typer.Option("--embedding-revision", help="Exact local model revision.")
    ] = "",
) -> None:
    """Embed only changed chunks using an explicit local model; never downloads."""
    if not str(embedding_model) or str(embedding_model) == ".":
        _error("Could not index knowledge", ValueError("--embedding-model is required"))
    try:
        root, _ = inspect_repository(repo)
        provider = load_local_embedding_provider(embedding_model, embedding_revision)
        result = index_embeddings(root, provider)
    except (OSError, WorktreeError, EmbeddingError, KnowledgeIngestError) as error:
        _error("Could not index knowledge", error)
    typer.echo(
        f"Indexed {result.embedded_chunks}/{result.total_chunks} chunk(s) with "
        f"{result.model_id} @ {result.revision} (dim {result.dimension}); "
        f"skipped {result.skipped_chunks} unchanged"
    )


@knowledge_app.command("search")
def knowledge_search(
    query: Annotated[str, typer.Argument(help="Natural-language or keyword query.")],
    repo: Annotated[Path, typer.Option("--repo", help="Target Git checkout root.")] = Path("."),
    tags: Annotated[
        list[str] | None, typer.Option("--tags", help="Require a tag; repeatable.")
    ] = None,
    stack: Annotated[
        list[str] | None, typer.Option("--stack", help="Require an applicable stack; repeatable.")
    ] = None,
    profile: Annotated[
        list[str] | None,
        typer.Option("--profile", help="Match an engineering profile; repeatable."),
    ] = None,
    source: Annotated[
        list[str] | None, typer.Option("--source", help="Limit to a source ID; repeatable.")
    ] = None,
    trust: Annotated[
        str | None, typer.Option("--trust", help="Minimum trust: low, medium, or high.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum results.")] = 6,
) -> None:
    """Hybrid citation search; retrieved text is reference data, never instructions."""
    try:
        root, _ = inspect_repository(repo)
        with KnowledgeStore(root, read_only=True) as store:
            provider = None
            state = _embedding_state(store)
            semantic_available = False
            if state is not None:
                try:
                    provider = load_local_embedding_provider(
                        str(state["model_path"]), str(state["revision"])
                    )
                    semantic_available = True
                except EmbeddingError:
                    provider = None
            filters = RetrievalFilters(
                trust=trust,
                tags=tuple(tags or ()),
                profiles=tuple(profile or ()),
                stacks=tuple(stack or ()),
                source_ids=tuple(source or ()),
            )
            results = hybrid_search(
                store, query, filters=filters, embedding_provider=provider, limit=limit
            )
    except (OSError, WorktreeError, KnowledgeStoreError) as error:
        _error("Could not search knowledge", error)
    if not semantic_available:
        typer.echo("Note: text indexed but semantic search unavailable; using lexical search.")
    if not results:
        typer.echo("No matching knowledge chunks.")
        return
    for rank, result in enumerate(results, start=1):
        citation = result.citation
        typer.echo(f"{rank}. [{citation.citation_id}] {citation.title}")
        typer.echo(
            f"   source: {citation.source_id} (trust={citation.trust}, license={citation.license})"
        )
        heading = " > ".join(citation.heading_path) if citation.heading_path else "(no heading)"
        typer.echo(f"   heading: {heading}")
        typer.echo(f"   locator: {citation.locator}")
        typer.echo(
            f"   ranks: lexical={result.lexical_rank} semantic={result.semantic_rank} "
            f"fused={result.fused_score:.6f}"
        )


__all__ = ["knowledge_app"]
