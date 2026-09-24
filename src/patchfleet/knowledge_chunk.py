"""Heading-aware normalization and chunking that keeps atomic structures intact."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from .knowledge_contracts import (
    CHUNK_OVERLAP_TOKENS,
    MAX_CHUNK_TOKENS,
    TARGET_CHUNK_TOKENS,
    ChunkRecord,
    estimate_tokens,
    stable_chunk_id,
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_RE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,9}[.)])\s+")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
ATOMIC_KINDS = frozenset({"code", "table", "list"})
LocatorFn = Callable[[int, int, tuple[str, ...]], str]


@dataclass(frozen=True)
class Block:
    kind: str
    text: str
    start_line: int
    end_line: int
    level: int = 0


def normalize_markdown(text: str) -> str:
    """Canonical line endings and blank-line spacing for stable hashing."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    blank = 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line == "":
            blank += 1
            if blank <= 1:
                lines.append("")
        else:
            blank = 0
            lines.append(line)
    return "\n".join(lines).strip("\n") + "\n"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:64] or "section"


def markdown_locator(base: str) -> LocatorFn:
    def locate(start: int, end: int, _heading_path: tuple[str, ...]) -> str:
        return f"{base}#L{start}-L{end}"

    return locate


def html_locator(base: str) -> LocatorFn:
    def locate(_start: int, _end: int, heading_path: tuple[str, ...]) -> str:
        if heading_path:
            return f"{base}#{slugify(heading_path[-1])}"
        return base

    return locate


def _fence(line: str) -> tuple[str, int] | None:
    match = FENCE_RE.match(line)
    if not match:
        return None
    token = match.group(1)
    return token[0], len(token)


def _is_closing_fence(line: str, char: str, length: int) -> bool:
    stripped = line.strip()
    return bool(stripped) and len(stripped) >= length and all(c == char for c in stripped)


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    return "|" in lines[index + 1] and TABLE_SEP_RE.match(lines[index + 1]) is not None


def _consume_list(lines: list[str], start: int) -> int:
    index = start
    count = len(lines)
    while index < count:
        line = lines[index]
        if line.strip() == "":
            look = index + 1
            while look < count and lines[look].strip() == "":
                look += 1
            if look < count and (LIST_RE.match(lines[look]) or lines[look][:1] in " \t"):
                index = look
                continue
            break
        if LIST_RE.match(line) or line[:1] in " \t":
            index += 1
            continue
        break
    return index


def parse_blocks(text: str) -> list[Block]:
    lines = text.split("\n")
    blocks: list[Block] = []
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        fence = _fence(line)
        if fence is not None:
            char, length = fence
            end = index + 1
            while end < count and not _is_closing_fence(lines[end], char, length):
                end += 1
            last = end if end < count else count - 1
            blocks.append(Block("code", "\n".join(lines[index : last + 1]), index + 1, last + 1))
            index = last + 1
            continue
        heading = HEADING_RE.match(line)
        if heading is not None:
            blocks.append(
                Block("heading", line.strip(), index + 1, index + 1, len(heading.group(1)))
            )
            index += 1
            continue
        if _is_table_start(lines, index):
            end = index
            while end < count and lines[end].strip() and "|" in lines[end]:
                end += 1
            blocks.append(Block("table", "\n".join(lines[index:end]), index + 1, end))
            index = end
            continue
        if LIST_RE.match(line):
            end = _consume_list(lines, index)
            blocks.append(Block("list", "\n".join(lines[index:end]), index + 1, end))
            index = end
            continue
        if line.lstrip().startswith(">"):
            end = index
            while end < count and lines[end].lstrip().startswith(">"):
                end += 1
            blocks.append(Block("blockquote", "\n".join(lines[index:end]), index + 1, end))
            index = end
            continue
        end = index
        while end < count and lines[end].strip():
            if (
                HEADING_RE.match(lines[end])
                or _fence(lines[end]) is not None
                or LIST_RE.match(lines[end])
                or _is_table_start(lines, end)
            ):
                break
            end += 1
        if end == index:
            end = index + 1
        blocks.append(Block("paragraph", "\n".join(lines[index:end]), index + 1, end))
        index = end
    return blocks


def _heading_name(block: Block) -> str:
    match = HEADING_RE.match(block.text)
    return match.group(2).strip() if match else block.text.strip()


def _split_prose(text: str, target_tokens: int) -> list[str]:
    limit = max(1, target_tokens) * 4
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in text.split():
        if current and length + len(word) + 1 > limit:
            pieces.append(" ".join(current))
            current = []
            length = 0
        current.append(word)
        length += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces or [text]


def _expand_prose(blocks: list[Block]) -> list[Block]:
    """Split oversized prose blocks at word boundaries; never split atomic blocks."""
    expanded: list[Block] = []
    for block in blocks:
        if block.kind in ATOMIC_KINDS or estimate_tokens(block.text) <= TARGET_CHUNK_TOKENS:
            expanded.append(block)
            continue
        for piece in _split_prose(block.text, TARGET_CHUNK_TOKENS):
            expanded.append(Block(block.kind, piece, block.start_line, block.end_line, block.level))
    return expanded


def _tail(text: str, tokens: int) -> str:
    limit = max(1, tokens) * 4
    if len(text) <= limit:
        return ""
    tail = text[-limit:]
    space = tail.find(" ")
    if space != -1:
        tail = tail[space + 1 :]
    return tail.strip()


def chunk_document(
    text: str,
    *,
    source_id: str,
    source_version: str,
    title: str,
    locator: LocatorFn,
    license: str,
    trust: str,
    tags: tuple[str, ...],
    profiles: tuple[str, ...],
    stacks: tuple[str, ...],
) -> list[ChunkRecord]:
    """Split normalized Markdown into heading-aware, evidence-locatable chunks."""
    blocks = _expand_prose(parse_blocks(normalize_markdown(text)))
    chunks: list[ChunkRecord] = []
    heading_stack: list[Block] = []
    pending: list[Block] = []
    pending_tokens = 0
    ordinal = 0
    carry_headings: list[Block] = []
    carry_text = ""

    def heading_path() -> tuple[str, ...]:
        return tuple(_heading_name(block) for block in heading_stack)

    def emit() -> str:
        nonlocal pending, pending_tokens, ordinal
        if not pending:
            return ""
        original = pending
        rendered = "\n\n".join(block.text for block in original).strip()
        pending = []
        pending_tokens = 0
        if not rendered:
            return ""
        starts = [block.start_line for block in original if block.start_line > 0]
        ends = [block.end_line for block in original if block.end_line > 0]
        start = min(starts) if starts else 1
        end = max(ends) if ends else start
        path = heading_path()
        digest = sha256(rendered.encode("utf-8")).hexdigest()
        chunks.append(
            ChunkRecord(
                chunk_id=stable_chunk_id(source_id, source_version, path, digest),
                snapshot_id="",
                source_id=source_id,
                source_version=source_version,
                title=title,
                heading_path=path,
                locator=locator(start, end, path),
                license=license,
                trust=trust,
                tags=tags,
                profiles=profiles,
                stacks=stacks,
                token_count=estimate_tokens(rendered),
                content_sha256=digest,
                ordinal=ordinal,
                text=rendered,
            )
        )
        ordinal += 1
        return rendered

    def set_carry(previous: str) -> None:
        nonlocal carry_headings, carry_text
        carry_headings = list(heading_stack)
        carry_text = _tail(previous, CHUNK_OVERLAP_TOKENS)

    def ensure_seed() -> None:
        nonlocal pending_tokens, carry_headings, carry_text
        if pending:
            return
        for heading in carry_headings:
            pending.append(heading)
            pending_tokens += estimate_tokens(heading.text)
        if carry_text:
            pending.append(Block("paragraph", carry_text, 0, 0))
            pending_tokens += estimate_tokens(carry_text)
        carry_headings = []
        carry_text = ""

    for block in blocks:
        if block.kind == "heading":
            emit()
            carry_headings = []
            carry_text = ""
            while heading_stack and heading_stack[-1].level >= block.level:
                heading_stack.pop()
            heading_stack.append(block)
            pending.append(block)
            pending_tokens += estimate_tokens(block.text)
            continue
        ensure_seed()
        block_tokens = estimate_tokens(block.text)
        if block.kind in ATOMIC_KINDS:
            if pending and pending_tokens + block_tokens > MAX_CHUNK_TOKENS:
                previous = emit()
                set_carry(previous)
                ensure_seed()
            pending.append(block)
            pending_tokens += block_tokens
            continue
        if pending and pending_tokens + block_tokens > MAX_CHUNK_TOKENS:
            previous = emit()
            set_carry(previous)
            ensure_seed()
        pending.append(block)
        pending_tokens += block_tokens
        if pending_tokens >= TARGET_CHUNK_TOKENS:
            previous = emit()
            set_carry(previous)
    emit()
    return chunks
