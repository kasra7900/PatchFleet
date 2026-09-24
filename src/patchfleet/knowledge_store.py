"""Local SQLite knowledge store: relational metadata, FTS5, and float32 vectors."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Self

from .knowledge_contracts import (
    KNOWLEDGE_DB_NAME,
    KNOWLEDGE_DIR,
    ChunkRecord,
    SnapshotRecord,
)
from .knowledge_registry import KnowledgeSource
from .worktrees import ensure_local_metadata


class KnowledgeStoreError(ValueError):
    """The local knowledge store is missing or unusable."""


def snapshot_id(source_id: str, raw_sha256: str) -> str:
    return "snap:" + sha256(f"{source_id}\x00{raw_sha256}".encode()).hexdigest()[:32]


def knowledge_directory(repository: Path) -> Path:
    return repository / KNOWLEDGE_DIR


def _dump(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _load(value: str) -> tuple[str, ...]:
    try:
        data = json.loads(value)
    except ValueError:
        return ()
    return tuple(str(item) for item in data) if isinstance(data, list) else ()


def _fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(f'"{term}"' for term in terms)


class KnowledgeStore:
    def __init__(self, repository: Path, *, read_only: bool = False) -> None:
        self.repository = Path(repository)
        self.directory = knowledge_directory(self.repository)
        self.database_path = self.directory / KNOWLEDGE_DB_NAME
        if read_only and not self.database_path.is_file():
            raise KnowledgeStoreError("no local knowledge store has been created")
        if not read_only:
            ensure_local_metadata(self.repository)
            self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        uri = f"file:{self.database_path}?mode=ro" if read_only else str(self.database_path)
        self._connection = sqlite3.connect(
            uri, uri=read_only, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._create_schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                license TEXT NOT NULL,
                trust TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                profiles_json TEXT NOT NULL,
                stacks_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                max_pages INTEGER NOT NULL,
                registry_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                source_version TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                normalized_sha256 TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                license TEXT NOT NULL,
                trust TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                profiles_json TEXT NOT NULL,
                stacks_json TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                UNIQUE (source_id, raw_sha256)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
                source_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                title TEXT NOT NULL,
                heading_path TEXT NOT NULL,
                locator TEXT NOT NULL,
                license TEXT NOT NULL,
                trust TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                profiles_json TEXT NOT NULL,
                stacks_json TEXT NOT NULL,
                token_count INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED, title, heading_path, text
            );
            CREATE TABLE IF NOT EXISTS embeddings (
                chunk_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                content_sha256 TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                PRIMARY KEY (chunk_id, model_id, revision)
            );
            CREATE TABLE IF NOT EXISTS index_state (
                model_id TEXT PRIMARY KEY,
                revision TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                model_path TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );
            """
        )

    def upsert_source(
        self, source: KnowledgeSource, registry_sha256: str, *, enabled: bool
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO sources (source_id, kind, license, trust, tags_json, profiles_json,
                    stacks_json, enabled, max_pages, registry_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    kind=excluded.kind, license=excluded.license, trust=excluded.trust,
                    tags_json=excluded.tags_json, profiles_json=excluded.profiles_json,
                    stacks_json=excluded.stacks_json, enabled=excluded.enabled,
                    max_pages=excluded.max_pages, registry_sha256=excluded.registry_sha256
                """,
                (
                    source.id,
                    source.kind,
                    source.license,
                    source.trust,
                    _dump(source.tags),
                    _dump(source.profiles),
                    _dump(source.stacks),
                    int(enabled),
                    source.max_pages,
                    registry_sha256,
                ),
            )

    def find_snapshot(self, source_id: str, raw_sha256: str) -> SnapshotRecord | None:
        row = self._connection.execute(
            "SELECT * FROM snapshots WHERE source_id = ? AND raw_sha256 = ?",
            (source_id, raw_sha256),
        ).fetchone()
        return self._row_to_snapshot(row) if row is not None else None

    def insert_snapshot(self, snapshot: SnapshotRecord) -> bool:
        """Insert once per (source, raw hash); an existing row is left untouched."""
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO snapshots (snapshot_id, source_id, source_version,
                    source_url, title, retrieved_at, raw_sha256, normalized_sha256,
                    warnings_json, license, trust, tags_json, profiles_json, stacks_json,
                    raw_path, normalized_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.source_id,
                    snapshot.source_version,
                    snapshot.source_url,
                    snapshot.title,
                    snapshot.retrieved_at,
                    snapshot.raw_sha256,
                    snapshot.normalized_sha256,
                    _dump(snapshot.warnings),
                    snapshot.license,
                    snapshot.trust,
                    _dump(snapshot.tags),
                    _dump(snapshot.profiles),
                    _dump(snapshot.stacks),
                    snapshot.raw_path,
                    snapshot.normalized_path,
                ),
            )
            return cursor.rowcount == 1

    def insert_chunks(self, chunks: Iterable[ChunkRecord]) -> int:
        inserted = 0
        with self._lock:
            for chunk in chunks:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO chunks (chunk_id, snapshot_id, source_id,
                        source_version, title, heading_path, locator, license, trust,
                        tags_json, profiles_json, stacks_json, token_count, content_sha256,
                        ordinal, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.snapshot_id,
                        chunk.source_id,
                        chunk.source_version,
                        chunk.title,
                        "\n".join(chunk.heading_path),
                        chunk.locator,
                        chunk.license,
                        chunk.trust,
                        _dump(chunk.tags),
                        _dump(chunk.profiles),
                        _dump(chunk.stacks),
                        chunk.token_count,
                        chunk.content_sha256,
                        chunk.ordinal,
                        chunk.text,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                inserted += 1
                self._connection.execute(
                    "INSERT INTO chunks_fts (chunk_id, title, heading_path, text) VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, chunk.title, "\n".join(chunk.heading_path), chunk.text),
                )
        return inserted

    def existing_embeddings(self, model_id: str, revision: str) -> dict[str, tuple[int, str]]:
        rows = self._connection.execute(
            "SELECT chunk_id, dimension, content_sha256 FROM embeddings "
            "WHERE model_id = ? AND revision = ?",
            (model_id, revision),
        ).fetchall()
        return {row["chunk_id"]: (row["dimension"], row["content_sha256"]) for row in rows}

    def store_embeddings(
        self,
        rows: Iterable[tuple[str, str, str, int, bytes, str, str]],
    ) -> int:
        stored = 0
        with self._lock:
            for chunk_id, model_id, revision, dimension, blob, content_sha256, indexed_at in rows:
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO embeddings (chunk_id, model_id, revision,
                        dimension, vector, content_sha256, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chunk_id, model_id, revision, dimension, blob, content_sha256, indexed_at),
                )
                stored += 1
        return stored

    def set_index_state(
        self, model_id: str, revision: str, dimension: int, model_path: str, indexed_at: str
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO index_state (model_id, revision, dimension,
                    model_path, indexed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (model_id, revision, dimension, model_path, indexed_at),
            )

    def get_index_state(self) -> dict[str, str | int] | None:
        row = self._connection.execute(
            "SELECT model_id, revision, dimension, model_path, indexed_at "
            "FROM index_state ORDER BY indexed_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def chunk_count_for_snapshot(self, snapshot_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return int(row["n"])

    def all_chunks(self) -> list[ChunkRecord]:
        rows = self._connection.execute("SELECT * FROM chunks ORDER BY chunk_id").fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def embedding_vectors(self, model_id: str, revision: str) -> list[tuple[str, bytes]]:
        rows = self._connection.execute(
            "SELECT chunk_id, vector FROM embeddings WHERE model_id = ? AND revision = ?",
            (model_id, revision),
        ).fetchall()
        return [(row["chunk_id"], row["vector"]) for row in rows]

    def lexical_candidates(self, query: str, *, limit: int) -> list[tuple[str, float]]:
        match = _fts_query(query)
        if not match:
            return []
        try:
            rows = self._connection.execute(
                """
                SELECT chunks_fts.chunk_id AS chunk_id, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks ON chunks.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ?
                ORDER BY bm25(chunks_fts) ASC, chunks_fts.chunk_id ASC
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(row["chunk_id"], float(row["score"])) for row in rows]

    def stats(self) -> dict[str, int]:
        def count(table: str) -> int:
            return int(
                self._connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )

        return {
            "sources": count("sources"),
            "snapshots": count("snapshots"),
            "chunks": count("chunks"),
            "embeddings": count("embeddings"),
        }

    def source_rows(self) -> list[dict[str, object]]:
        rows = self._connection.execute(
            """
            SELECT s.source_id, s.kind, s.enabled, s.trust, s.license,
                   (SELECT COUNT(*) FROM snapshots sn WHERE sn.source_id = s.source_id)
                       AS snapshot_count,
                   (SELECT COUNT(*) FROM chunks c WHERE c.source_id = s.source_id) AS chunk_count,
                   (SELECT COUNT(*) FROM embeddings e JOIN chunks c ON c.chunk_id = e.chunk_id
                        WHERE c.source_id = s.source_id) AS embedded_chunk_count,
                   (SELECT MAX(sn.retrieved_at) FROM snapshots sn WHERE sn.source_id = s.source_id)
                       AS latest_retrieved_at,
                   (SELECT sn.source_version FROM snapshots sn WHERE sn.source_id = s.source_id
                        ORDER BY sn.retrieved_at DESC LIMIT 1) AS latest_source_version
            FROM sources s ORDER BY s.source_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SnapshotRecord:
        return SnapshotRecord(
            snapshot_id=row["snapshot_id"],
            source_id=row["source_id"],
            source_version=row["source_version"],
            source_url=row["source_url"],
            title=row["title"],
            retrieved_at=row["retrieved_at"],
            raw_sha256=row["raw_sha256"],
            normalized_sha256=row["normalized_sha256"],
            warnings=_load(row["warnings_json"]),
            license=row["license"],
            trust=row["trust"],
            tags=_load(row["tags_json"]),
            profiles=_load(row["profiles_json"]),
            stacks=_load(row["stacks_json"]),
            raw_path=row["raw_path"],
            normalized_path=row["normalized_path"],
        )

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> ChunkRecord:
        return ChunkRecord(
            chunk_id=row["chunk_id"],
            snapshot_id=row["snapshot_id"],
            source_id=row["source_id"],
            source_version=row["source_version"],
            title=row["title"],
            heading_path=tuple(part for part in row["heading_path"].split("\n") if part),
            locator=row["locator"],
            license=row["license"],
            trust=row["trust"],
            tags=_load(row["tags_json"]),
            profiles=_load(row["profiles_json"]),
            stacks=_load(row["stacks_json"]),
            token_count=row["token_count"],
            content_sha256=row["content_sha256"],
            ordinal=row["ordinal"],
            text=row["text"],
        )
