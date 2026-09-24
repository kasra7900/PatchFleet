"""Deterministic hybrid retrieval: FTS5 lexical plus local semantic, fused by RRF."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .knowledge_contracts import (
    TRUST_RANK,
    ChunkRecord,
    KnowledgeCitation,
)
from .knowledge_embed import EmbeddingProvider, blob_to_vector, cosine_similarity
from .knowledge_store import KnowledgeStore

DEFAULT_RESULT_LIMIT = 6
DEFAULT_CANDIDATE_LIMIT = 50
RRF_K = 60


@dataclass(frozen=True)
class RetrievalFilters:
    trust: str | None = None
    tags: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    stacks: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()

    def matches(self, chunk: ChunkRecord) -> bool:
        if self.trust is not None and TRUST_RANK.get(chunk.trust, -1) < TRUST_RANK.get(
            self.trust, 0
        ):
            return False
        if self.tags and not set(self.tags).issubset(set(chunk.tags)):
            return False
        if self.profiles and not set(self.profiles).intersection(chunk.profiles):
            return False
        if self.stacks and not set(self.stacks).intersection(chunk.stacks):
            return False
        return not (self.source_ids and chunk.source_id not in self.source_ids)


@dataclass(frozen=True)
class RetrievalResult:
    citation: KnowledgeCitation
    fused_score: float
    lexical_rank: int | None = None
    semantic_rank: int | None = None


def _filtered_chunks(store: KnowledgeStore, filters: RetrievalFilters) -> dict[str, ChunkRecord]:
    return {chunk.chunk_id: chunk for chunk in store.all_chunks() if filters.matches(chunk)}


def hybrid_search(
    store: KnowledgeStore,
    query: str,
    *,
    filters: RetrievalFilters | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    rrf_k: int = RRF_K,
) -> list[RetrievalResult]:
    """Combine lexical and semantic candidates; results are reference data only."""
    active_filters = filters if filters is not None else RetrievalFilters()
    chunks = _filtered_chunks(store, active_filters)

    lexical: list[tuple[str, float]] = [
        (chunk_id, score)
        for chunk_id, score in store.lexical_candidates(query, limit=candidate_limit)
        if chunk_id in chunks
    ][:candidate_limit]

    semantic: list[tuple[str, float]] = []
    if embedding_provider is not None and query.strip():
        query_vector = embedding_provider.encode_query(query)
        scored: list[tuple[str, float]] = []
        for chunk_id, blob in store.embedding_vectors(
            embedding_provider.model_id, embedding_provider.revision
        ):
            if chunk_id not in chunks:
                continue
            scored.append((chunk_id, cosine_similarity(query_vector, blob_to_vector(blob))))
        scored.sort(key=lambda item: (-item[1], item[0]))
        semantic = scored[:candidate_limit]

    scores: defaultdict[str, float] = defaultdict(float)
    lexical_rank: dict[str, int] = {}
    semantic_rank: dict[str, int] = {}
    for rank, (chunk_id, _score) in enumerate(lexical, start=1):
        scores[chunk_id] += 1.0 / (rrf_k + rank)
        lexical_rank.setdefault(chunk_id, rank)
    for rank, (chunk_id, _score) in enumerate(semantic, start=1):
        scores[chunk_id] += 1.0 / (rrf_k + rank)
        semantic_rank.setdefault(chunk_id, rank)

    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
    return [
        RetrievalResult(
            citation=KnowledgeCitation.from_chunk(chunks[chunk_id]),
            fused_score=scores[chunk_id],
            lexical_rank=lexical_rank.get(chunk_id),
            semantic_rank=semantic_rank.get(chunk_id),
        )
        for chunk_id in ordered
    ]
