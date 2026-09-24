"""Immutable knowledge records, citations, and stable identifiers.

This module intentionally depends only on the standard library so it can be
imported by the Leader prompt compiler and the Architecture Gate without
pulling in parsing or embedding dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

KNOWLEDGE_DIR = ".patchfleet/knowledge"
KNOWLEDGE_DB_NAME = "knowledge.sqlite3"
SCHEMA_VERSION = "0.1"

TARGET_CHUNK_TOKENS = 450
MAX_CHUNK_TOKENS = 700
CHUNK_OVERLAP_TOKENS = 60

TRUST_LEVELS = ("low", "medium", "high")
TRUST_RANK = {"low": 0, "medium": 1, "high": 2}


def estimate_tokens(text: str) -> int:
    """Deterministic approximate token count; good enough for chunk sizing."""
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def stable_chunk_id(
    source_id: str, source_version: str, heading_path: tuple[str, ...], content_sha256: str
) -> str:
    """Stable ID derived from source version, heading path, and content hash."""
    seed = "\x00".join([source_id, source_version, "/".join(heading_path), content_sha256])
    return "chunk:" + sha256(seed.encode("utf-8")).hexdigest()[:32]


def knowledge_evidence_id(chunk_id: str) -> str:
    """Evidence reference ID understood by the Architecture Gate."""
    return f"knowledge:{chunk_id}"


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    source_id: str
    source_version: str
    source_url: str
    title: str
    retrieved_at: str
    raw_sha256: str
    normalized_sha256: str
    warnings: tuple[str, ...]
    license: str
    trust: str
    tags: tuple[str, ...]
    profiles: tuple[str, ...]
    stacks: tuple[str, ...]
    raw_path: str
    normalized_path: str


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    snapshot_id: str
    source_id: str
    source_version: str
    title: str
    heading_path: tuple[str, ...]
    locator: str
    license: str
    trust: str
    tags: tuple[str, ...]
    profiles: tuple[str, ...]
    stacks: tuple[str, ...]
    token_count: int
    content_sha256: str
    ordinal: int
    text: str

    @property
    def evidence_id(self) -> str:
        return knowledge_evidence_id(self.chunk_id)


@dataclass(frozen=True)
class KnowledgeCitation:
    chunk_id: str
    source_id: str
    source_version: str
    title: str
    heading_path: tuple[str, ...]
    locator: str
    license: str
    trust: str
    tags: tuple[str, ...]
    profiles: tuple[str, ...]
    stacks: tuple[str, ...]
    token_count: int
    content_sha256: str
    text: str

    @property
    def citation_id(self) -> str:
        return knowledge_evidence_id(self.chunk_id)

    @classmethod
    def from_chunk(cls, chunk: ChunkRecord) -> KnowledgeCitation:
        return cls(
            chunk_id=chunk.chunk_id,
            source_id=chunk.source_id,
            source_version=chunk.source_version,
            title=chunk.title,
            heading_path=chunk.heading_path,
            locator=chunk.locator,
            license=chunk.license,
            trust=chunk.trust,
            tags=chunk.tags,
            profiles=chunk.profiles,
            stacks=chunk.stacks,
            token_count=chunk.token_count,
            content_sha256=chunk.content_sha256,
            text=chunk.text,
        )


@dataclass(frozen=True)
class SourceStatus:
    source_id: str
    kind: str
    enabled: bool
    trust: str
    license: str
    snapshot_count: int
    chunk_count: int
    embedded_chunk_count: int
    latest_retrieved_at: str | None
    latest_source_version: str | None


@dataclass(frozen=True)
class KnowledgeStatus:
    registry_present: bool
    source_count: int
    enabled_source_count: int
    snapshot_count: int
    chunk_count: int
    embedded_chunk_count: int
    embedding_model: str | None
    embedding_revision: str | None
    semantic_available: bool
    sources: tuple[SourceStatus, ...]

    @property
    def semantic_message(self) -> str:
        if self.chunk_count == 0:
            return "no text indexed"
        if not self.semantic_available:
            return "text indexed but semantic search unavailable"
        return "semantic search available"
