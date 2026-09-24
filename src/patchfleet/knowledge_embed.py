"""Local embedding providers. No model is ever downloaded automatically."""

from __future__ import annotations

import array
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable


class EmbeddingError(ValueError):
    """A local embedding model cannot be used as configured."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    model_id: str
    revision: str
    dimension: int

    def encode_document(self, text: str) -> list[float]: ...

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


def vector_to_blob(vector: Sequence[float]) -> bytes:
    return array.array("f", vector).tobytes()


def blob_to_vector(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


class LocalSentenceTransformerProvider:
    """Loads an already-present local model directory; never contacts a hub."""

    def __init__(self, model_path: str | Path, revision: str) -> None:
        path = Path(model_path).expanduser()
        if not path.is_dir() or path.is_symlink():
            raise EmbeddingError(
                f"embedding model path must be an existing local directory: {path}"
            )
        if not revision.strip():
            raise EmbeddingError("an exact embedding model revision is required")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise EmbeddingError(
                "sentence-transformers is not installed; install patchfleet[knowledge]"
            ) from error
        self.model_id = str(path.resolve())
        self.revision = revision.strip()
        try:
            self._model = SentenceTransformer(str(path.resolve()))
        except (OSError, ValueError, RuntimeError) as error:
            raise EmbeddingError(f"could not load local embedding model: {error}") from error
        dimension = self._model.get_sentence_embedding_dimension()
        if not isinstance(dimension, int) or dimension <= 0:
            raise EmbeddingError("embedding model did not report a valid dimension")
        self.dimension = dimension

    def encode_document(self, text: str) -> list[float]:
        return self.encode_documents([text])[0]

    def encode_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(value) for value in vector] for vector in vectors]

    def encode_query(self, text: str) -> list[float]:
        return self.encode_documents([text])[0]


def load_local_embedding_provider(model_path: str | Path, revision: str) -> EmbeddingProvider:
    """Explicit factory; tests substitute a deterministic fake provider here."""
    return LocalSentenceTransformerProvider(model_path, revision)
