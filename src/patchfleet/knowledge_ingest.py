"""Controlled, local-first ingestion into immutable knowledge snapshots."""

from __future__ import annotations

import html
import re
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit

from .knowledge_chunk import (
    chunk_document,
    html_locator,
    markdown_locator,
    normalize_markdown,
)
from .knowledge_contracts import ChunkRecord, SnapshotRecord
from .knowledge_embed import EmbeddingProvider, vector_to_blob
from .knowledge_registry import (
    KnowledgeRegistry,
    KnowledgeSource,
    domain_allowed,
    find_source,
    load_registry,
    registry_fingerprint,
)
from .knowledge_store import KnowledgeStore, snapshot_id
from .worktrees import ensure_local_metadata, inspect_repository

USER_AGENT = "PatchFleet/0.0.0 (+local-knowledge-ingestion; contact: none)"
FETCH_TIMEOUT = 20.0
MAX_HTML_BYTES = 2_000_000
MAX_MARKDOWN_BYTES = 1_000_000
MAX_REDIRECTS = 5
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
EMBED_BATCH = 32


class KnowledgeIngestError(ValueError):
    """Ingestion is out of scope, unconfirmed, or unsafe."""


@dataclass(frozen=True)
class FetchedPage:
    final_url: str
    raw: bytes
    text: str


@dataclass(frozen=True)
class Extraction:
    title: str
    markdown: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class IngestResult:
    source_id: str
    kind: str
    snapshot_id: str
    source_version: str
    title: str
    source_url: str
    raw_sha256: str
    normalized_sha256: str
    chunk_count: int
    skipped: bool
    warnings: tuple[str, ...]
    retrieved_at: str


@dataclass(frozen=True)
class IndexResult:
    model_id: str
    revision: str
    dimension: int
    total_chunks: int
    embedded_chunks: int
    skipped_chunks: int


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _check_url(url: str, domains: tuple[str, ...]) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme == "https"
        or parts.scheme == "http"
        and (host in {"localhost", "127.0.0.1", "::1", "[::1]"} or host.endswith(".localhost"))
    ):
        pass
    else:
        raise KnowledgeIngestError(f"refusing non-HTTPS remote URL: {url}")
    if not domain_allowed(host, domains):
        raise KnowledgeIngestError(f"URL host is outside the allowlisted domains: {url}")


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _build_opener() -> urllib_request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib_request.build_opener(_NoRedirect(), urllib_request.HTTPSHandler(context=context))


def _fetch_html(
    url: str,
    domains: tuple[str, ...],
    *,
    opener: urllib_request.OpenerDirector | None = None,
    timeout: float = FETCH_TIMEOUT,
    max_bytes: int = MAX_HTML_BYTES,
) -> FetchedPage:
    """Retrieve exactly one page, validating each redirect against the allowlist."""
    active = opener or _build_opener()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _check_url(current, domains)
        request = urllib_request.Request(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        try:
            response = active.open(request, timeout=timeout)
        except HTTPError as error:
            location = error.headers.get("Location") if error.headers else None
            if error.code in REDIRECT_CODES and location:
                current = urljoin(current, location)
                continue
            raise KnowledgeIngestError(f"HTTP {error.code} for {current}") from error
        except (URLError, OSError, TimeoutError) as error:
            raise KnowledgeIngestError(f"could not retrieve {current}: {error}") from error
        with response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise KnowledgeIngestError("remote page exceeds the size limit")
            final_url = response.geturl()
            _check_url(final_url, domains)
            charset = response.headers.get_content_charset() or "utf-8"
            content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise KnowledgeIngestError(f"unsupported remote content type: {content_type}")
        return FetchedPage(final_url, data, data.decode(charset, "replace"))
    raise KnowledgeIngestError("too many redirects")


class _HtmlToMarkdown(HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "blockquote", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._pre = 0
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre += 1
            self.parts.append("\n\n```\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCK or tag in {"ul", "ol", "tr", "br"}:
            self.parts.append("\n\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self.parts.append("[")
        elif tag == "code" and not self._pre:
            self.parts.append("`")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.parts.append("\n```\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
        elif tag == "a":
            href = self._href
            self._href = None
            self.parts.append(f"]({href})" if href else "]")
        elif tag == "code" and not self._pre:
            self.parts.append("`")
        elif tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data if self._pre else data)

    def markdown(self) -> str:
        return "".join(self.parts)


def _html_title(text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if match is None:
        return ""
    return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def _first_heading(markdown: str) -> str:
    match = re.search(r"^#{1,6}\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _basic_html_to_markdown(text: str) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(text)
    parser.close()
    return parser.markdown()


def _extract_html(text: str, url: str) -> Extraction:
    warnings: list[str] = []
    title = _html_title(text)
    try:
        import trafilatura
    except ImportError:
        warnings.append("trafilatura_unavailable: used conservative fallback extractor")
    else:
        try:
            extracted = trafilatura.extract(
                text,
                url=url,
                output_format="markdown",
                include_links=True,
                include_tables=True,
                include_comments=False,
            )
        except Exception as error:  # noqa: BLE001 - extraction libraries vary widely
            extracted = None
            warnings.append(f"trafilatura_error: {type(error).__name__}")
        if extracted and extracted.strip():
            return Extraction(title or _first_heading(extracted) or url, extracted, tuple(warnings))
        warnings.append("trafilatura_empty: no main content extracted")
    markdown = _basic_html_to_markdown(text)
    if not markdown.strip():
        warnings.append("empty_content: no readable text extracted")
    return Extraction(title or _first_heading(markdown) or url, markdown, tuple(warnings))


def _git_bytes(repo: Path, *args: str, timeout: float = 120.0) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise KnowledgeIngestError(f"Git unavailable: {error}") from error
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise KnowledgeIngestError(f"Git operation failed: {message[:300]}")
    return result.stdout


def _safe_markdown_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and all(part not in ("", ".", "..") for part in parts)


def _read_tracked_markdown(repository: Path, path: str) -> tuple[bytes, str]:
    if not _safe_markdown_path(path):
        raise KnowledgeIngestError(f"unsafe Markdown path: {path}")
    from .context import _excluded

    if _excluded(path):
        raise KnowledgeIngestError(f"refusing to ingest sensitive path: {path}")
    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--", path],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if tracked.returncode:
        raise KnowledgeIngestError(f"Markdown source is not a tracked file: {path}")
    target = repository / path
    if target.is_symlink() or not target.is_file():
        raise KnowledgeIngestError(f"Markdown source must be a regular file: {path}")
    data = target.read_bytes()
    if len(data) > MAX_MARKDOWN_BYTES:
        raise KnowledgeIngestError("Markdown source exceeds the size limit")
    blob = _git_bytes(repository, "hash-object", "--", path).decode().strip()
    return data, blob


def _fetch_git_files(
    url: str, revision: str, paths: tuple[str, ...], *, timeout: float
) -> list[tuple[str, bytes]]:
    with tempfile.TemporaryDirectory(prefix="patchfleet-knowledge-") as temporary:
        clone = Path(temporary) / "clone"
        try:
            result = subprocess.run(
                ["git", "clone", "--quiet", "--no-checkout", url, str(clone)],
                check=False,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KnowledgeIngestError(f"Git clone failed: {error}") from error
        if result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise KnowledgeIngestError(f"Git clone failed: {message[:300]}")
        _git_bytes(clone, "rev-parse", "--verify", f"{revision}^{{commit}}")
        documents: list[tuple[str, bytes]] = []
        for path in paths:
            if not _safe_markdown_path(path):
                raise KnowledgeIngestError(f"unsafe Git path: {path}")
            documents.append((path, _git_bytes(clone, "show", f"{revision}:{path}")))
        return documents


def _write_immutable(path: Path, data: bytes) -> None:
    """Never overwrite historical snapshots; identical re-writes are allowed."""
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise KnowledgeIngestError(f"refused to overwrite immutable snapshot: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as target:
        target.write(data)
    path.chmod(0o600)


def _snapshot_paths(repository: Path, source_id: str, raw_sha256: str) -> tuple[Path, Path]:
    base = repository / ".patchfleet" / "knowledge" / "snapshots" / source_id
    return base / f"{raw_sha256}.raw", base / f"{raw_sha256}.md"


def _relative(repository: Path, path: Path) -> str:
    return str(path.relative_to(repository).as_posix())


def _persist_document(
    repository: Path,
    store: KnowledgeStore,
    source: KnowledgeSource,
    *,
    raw: bytes,
    normalized: str,
    title: str,
    source_url: str,
    source_version: str,
    warnings: tuple[str, ...],
    locator,
    retrieved_at: str,
) -> IngestResult:
    raw_sha = sha256(raw).hexdigest()
    existing = store.find_snapshot(source.id, raw_sha)
    if existing is not None:
        return IngestResult(
            source_id=source.id,
            kind=source.kind,
            snapshot_id=existing.snapshot_id,
            source_version=existing.source_version,
            title=existing.title,
            source_url=existing.source_url,
            raw_sha256=existing.raw_sha256,
            normalized_sha256=existing.normalized_sha256,
            chunk_count=store.chunk_count_for_snapshot(existing.snapshot_id),
            skipped=True,
            warnings=existing.warnings,
            retrieved_at=existing.retrieved_at,
        )
    normalized_bytes = normalized.encode("utf-8")
    normalized_sha = sha256(normalized_bytes).hexdigest()
    raw_path, normalized_path = _snapshot_paths(repository, source.id, raw_sha)
    _write_immutable(raw_path, raw)
    _write_immutable(normalized_path, normalized_bytes)
    record = SnapshotRecord(
        snapshot_id=snapshot_id(source.id, raw_sha),
        source_id=source.id,
        source_version=source_version,
        source_url=source_url,
        title=title,
        retrieved_at=retrieved_at,
        raw_sha256=raw_sha,
        normalized_sha256=normalized_sha,
        warnings=warnings,
        license=source.license,
        trust=source.trust,
        tags=source.tags,
        profiles=source.profiles,
        stacks=source.stacks,
        raw_path=_relative(repository, raw_path),
        normalized_path=_relative(repository, normalized_path),
    )
    inserted = store.insert_snapshot(record)
    chunks = chunk_document(
        normalized,
        source_id=source.id,
        source_version=source_version,
        title=title,
        locator=locator,
        license=source.license,
        trust=source.trust,
        tags=source.tags,
        profiles=source.profiles,
        stacks=source.stacks,
    )
    filled = [
        ChunkRecord(**{**chunk.__dict__, "snapshot_id": record.snapshot_id}) for chunk in chunks
    ]
    store.insert_chunks(filled)
    return IngestResult(
        source_id=source.id,
        kind=source.kind,
        snapshot_id=record.snapshot_id,
        source_version=source_version,
        title=title,
        source_url=source_url,
        raw_sha256=raw_sha,
        normalized_sha256=normalized_sha,
        chunk_count=len(filled),
        skipped=not inserted,
        warnings=warnings,
        retrieved_at=retrieved_at,
    )


def _markdown_title(text: str, path: str) -> str:
    heading = _first_heading(text)
    if heading:
        return heading
    return PurePosixPath(path).stem


def ingest_source(
    repository: Path,
    source_id: str,
    *,
    confirm: bool,
    registry: KnowledgeRegistry | None = None,
    now: datetime | None = None,
) -> tuple[IngestResult, ...]:
    """Ingest one explicitly configured source; no implicit crawling or discovery."""
    root, _ = inspect_repository(repository)
    loaded = registry if registry is not None else load_registry(root)
    source = find_source(loaded, source_id)
    if not source.enabled:
        raise KnowledgeIngestError(f"source '{source_id}' is disabled; enable it explicitly")
    if source.kind in {"html", "git"} and not confirm:
        raise KnowledgeIngestError("network ingestion requires the explicit --confirm flag")
    ensure_local_metadata(root)
    retrieved_at = _now(now).isoformat()
    fingerprint = registry_fingerprint(loaded)
    with KnowledgeStore(root) as store:
        store.upsert_source(source, fingerprint, enabled=source.enabled)
        return _dispatch(root, store, source, retrieved_at)


def _dispatch(
    root: Path, store: KnowledgeStore, source: KnowledgeSource, retrieved_at: str
) -> tuple[IngestResult, ...]:
    if source.kind == "markdown":
        raw, blob = _read_tracked_markdown(root, source.path)
        normalized = normalize_markdown(raw.decode("utf-8", "replace"))
        title = _markdown_title(normalized, source.path)
        return (
            _persist_document(
                root,
                store,
                source,
                raw=raw,
                normalized=normalized,
                title=title,
                source_url=source.path,
                source_version=f"blob:{blob}",
                warnings=(),
                locator=markdown_locator(source.path),
                retrieved_at=retrieved_at,
            ),
        )
    if source.kind == "html":
        page = _fetch_html(source.url, source.domains)
        extraction = _extract_html(page.text, page.final_url)
        normalized = normalize_markdown(extraction.markdown)
        return (
            _persist_document(
                root,
                store,
                source,
                raw=page.raw,
                normalized=normalized,
                title=extraction.title,
                source_url=page.final_url,
                source_version=f"content:{sha256(page.raw).hexdigest()}",
                warnings=extraction.warnings,
                locator=html_locator(page.final_url),
                retrieved_at=retrieved_at,
            ),
        )
    results: list[IngestResult] = []
    for path, data in _fetch_git_files(source.url, source.revision, source.paths, timeout=120.0):
        normalized = normalize_markdown(data.decode("utf-8", "replace"))
        results.append(
            _persist_document(
                root,
                store,
                source,
                raw=data,
                normalized=normalized,
                title=_markdown_title(normalized, path),
                source_url=f"{source.url}@{source.revision}#{path}",
                source_version=f"{source.revision}:{path}",
                warnings=(),
                locator=markdown_locator(path),
                retrieved_at=retrieved_at,
            )
        )
    return tuple(results)


def index_embeddings(
    repository: Path,
    provider: EmbeddingProvider,
    *,
    now: datetime | None = None,
) -> IndexResult:
    """Embed only changed/new chunks for the configured local model."""
    root, _ = inspect_repository(repository)
    ensure_local_metadata(root)
    timestamp = _now(now).isoformat()
    with KnowledgeStore(root) as store:
        chunks = store.all_chunks()
        existing = store.existing_embeddings(provider.model_id, provider.revision)
        pending = [
            chunk
            for chunk in chunks
            if existing.get(chunk.chunk_id) != (provider.dimension, chunk.content_sha256)
        ]
        stored = 0
        for start in range(0, len(pending), EMBED_BATCH):
            batch = pending[start : start + EMBED_BATCH]
            vectors = provider.encode_documents([chunk.text for chunk in batch])
            rows = []
            for chunk, vector in zip(batch, vectors, strict=True):
                if len(vector) != provider.dimension:
                    raise KnowledgeIngestError("embedding dimension does not match the provider")
                rows.append(
                    (
                        chunk.chunk_id,
                        provider.model_id,
                        provider.revision,
                        provider.dimension,
                        vector_to_blob(vector),
                        chunk.content_sha256,
                        timestamp,
                    )
                )
            stored += store.store_embeddings(rows)
        store.set_index_state(
            provider.model_id, provider.revision, provider.dimension, provider.model_id, timestamp
        )
    return IndexResult(
        model_id=provider.model_id,
        revision=provider.revision,
        dimension=provider.dimension,
        total_chunks=len(chunks),
        embedded_chunks=stored,
        skipped_chunks=len(chunks) - len(pending),
    )
