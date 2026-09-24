"""Tracked, license-aware source registry for local engineering knowledge."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import Field, ValidationError, field_validator

from .contracts import Identifier, StrictModel

REGISTRY_NAME = "patchfleet.knowledge.yaml"
MAX_REGISTRY_BYTES = 65536
LICENSE_PLACEHOLDERS = {"", "unknown", "none", "unlicensed", "tbd", "n/a", "na", "todo"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class KnowledgeRegistryError(ValueError):
    def __init__(self, issues: list[RegistryIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in issues))


@dataclass(frozen=True)
class RegistryIssue:
    path: str
    code: str
    message: str


class _SourceBase(StrictModel):
    id: Identifier
    license: str
    trust: Literal["low", "medium", "high"]
    tags: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    stacks: tuple[str, ...] = ()
    enabled: bool = Field(strict=True)
    max_pages: int = Field(ge=1, le=100, strict=True)

    @field_validator("license")
    @classmethod
    def nonblank_license(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("license must not be blank")
        return value

    @field_validator("tags", "profiles", "stacks")
    @classmethod
    def nonblank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        items = tuple(value.strip() for value in values)
        if any(not item for item in items):
            raise ValueError("list entries must not be blank")
        if len(items) != len(set(items)):
            raise ValueError("list entries must be unique")
        return items


class MarkdownSource(_SourceBase):
    kind: Literal["markdown"]
    path: str


class HtmlSource(_SourceBase):
    kind: Literal["html"]
    url: str
    domains: tuple[str, ...] = Field(min_length=1)


class GitSource(_SourceBase):
    kind: Literal["git"]
    url: str
    revision: str
    paths: tuple[str, ...] = Field(min_length=1)


KnowledgeSource = Annotated[MarkdownSource | HtmlSource | GitSource, Field(discriminator="kind")]


class KnowledgeRegistry(StrictModel):
    schema_version: Literal["0.1"]
    sources: tuple[KnowledgeSource, ...]

    @field_validator("sources")
    @classmethod
    def distinct_ids(cls, values: tuple[KnowledgeSource, ...]) -> tuple[KnowledgeSource, ...]:
        ids = [value.id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("source IDs must be unique")
        return values


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.endswith(".localhost")


def domain_allowed(host: str, domains: tuple[str, ...]) -> bool:
    host = host.casefold()
    return any(
        host == domain.casefold() or host.endswith("." + domain.casefold()) for domain in domains
    )


def _safe_relative_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or any(ord(c) < 32 for c in path):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and all(part not in ("", ".", "..") for part in parts)


def _sensitive(path: str) -> bool:
    from .context import _excluded

    return _excluded(path)


def registry_fingerprint(registry: KnowledgeRegistry) -> str:
    data = registry.model_dump(mode="json")
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_registry(value: object, repository: Path | None = None) -> KnowledgeRegistry:
    try:
        registry = KnowledgeRegistry.model_validate(value)
    except ValidationError as error:
        raise KnowledgeRegistryError(
            [
                RegistryIssue(
                    ".".join(map(str, item["loc"])) or "registry", item["type"], item["msg"]
                )
                for item in error.errors()
            ]
        ) from error

    issues: list[RegistryIssue] = []
    known_profiles = set(_profile_ids())

    for index, source in enumerate(registry.sources):
        base = f"sources[{index}]"
        if source.license.strip().casefold() in LICENSE_PLACEHOLDERS:
            issues.append(
                RegistryIssue(f"{base}.license", "license_required", "declare a real license")
            )
        for profile_id in source.profiles:
            if profile_id not in known_profiles:
                issues.append(
                    RegistryIssue(
                        f"{base}.profiles",
                        "unknown_profile",
                        f"unknown engineering profile: {profile_id}",
                    )
                )
        if isinstance(source, MarkdownSource):
            _validate_markdown(source, base, repository, issues)
        elif isinstance(source, HtmlSource):
            _validate_html(source, base, issues)
        elif isinstance(source, GitSource):
            _validate_git(source, base, issues)
    if issues:
        raise KnowledgeRegistryError(issues)
    return registry


def _profile_ids() -> tuple[str, ...]:
    from .profiles import CATALOGUE

    return tuple(CATALOGUE)


def _validate_markdown(
    source: MarkdownSource, base: str, repository: Path | None, issues: list[RegistryIssue]
) -> None:
    if source.max_pages != 1:
        issues.append(
            RegistryIssue(
                f"{base}.max_pages", "scope", "local Markdown ingestion supports exactly one file"
            )
        )
    if not _safe_relative_path(source.path) or _sensitive(source.path):
        issues.append(
            RegistryIssue(
                f"{base}.path",
                "unsafe_path",
                "path must be a safe repository-relative tracked Markdown file",
            )
        )
        return
    if PurePosixPath(source.path).suffix.casefold() != ".md":
        issues.append(RegistryIssue(f"{base}.path", "not_markdown", "path must end in .md"))
        return
    if repository is not None:
        _require_tracked(repository, source.path, base, issues)


def _validate_html(source: HtmlSource, base: str, issues: list[RegistryIssue]) -> None:
    if source.max_pages != 1:
        issues.append(
            RegistryIssue(f"{base}.max_pages", "scope", "HTML ingestion supports one explicit page")
        )
    parts = urlsplit(source.url)
    host = _host(source.url)
    if parts.scheme == "https" or parts.scheme == "http" and _is_loopback(host):
        pass
    else:
        issues.append(RegistryIssue(f"{base}.url", "scheme", "remote HTML sources must use HTTPS"))
    if not host:
        issues.append(RegistryIssue(f"{base}.url", "canonical_url", "canonical URL is required"))
    if not source.domains:
        issues.append(
            RegistryIssue(f"{base}.domains", "domains_required", "allowlist at least one domain")
        )
    elif host and not domain_allowed(host, source.domains):
        issues.append(
            RegistryIssue(
                f"{base}.domains", "out_of_scope", "canonical URL host is not in allowed domains"
            )
        )


def _validate_git(source: GitSource, base: str, issues: list[RegistryIssue]) -> None:
    if not source.url.strip():
        issues.append(RegistryIssue(f"{base}.url", "repository_url", "repository URL is required"))
    if len(source.revision) != 40 or any(c not in "0123456789abcdef" for c in source.revision):
        issues.append(
            RegistryIssue(
                f"{base}.revision", "revision", "pin a full 40-character lowercase commit SHA"
            )
        )
    if len(source.paths) > source.max_pages:
        issues.append(
            RegistryIssue(
                f"{base}.paths", "max_pages", "explicit paths exceed the configured max_pages"
            )
        )
    for path in source.paths:
        if not _safe_relative_path(path) or _sensitive(path):
            issues.append(
                RegistryIssue(f"{base}.paths", "unsafe_path", f"unsafe repository path: {path}")
            )
        elif PurePosixPath(path).suffix.casefold() != ".md":
            issues.append(
                RegistryIssue(f"{base}.paths", "not_markdown", f"path must end in .md: {path}")
            )


def _require_tracked(repository: Path, path: str, base: str, issues: list[RegistryIssue]) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--", path],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if result.returncode or not (repository / path).is_file():
        issues.append(RegistryIssue(f"{base}.path", "untracked", f"not a tracked file: {path}"))


def registry_path(repository: Path, name: str = REGISTRY_NAME) -> Path:
    candidate = Path(name)
    return candidate if candidate.is_absolute() else repository / candidate


def load_registry(repository: Path, name: str = REGISTRY_NAME) -> KnowledgeRegistry:
    path = registry_path(repository, name)
    if not path.is_file() or path.is_symlink():
        raise KnowledgeRegistryError(
            [
                RegistryIssue(
                    name,
                    "missing",
                    "create a tracked patchfleet.knowledge.yaml; none is created for you",
                )
            ]
        )
    if path.stat().st_size > MAX_REGISTRY_BYTES:
        raise KnowledgeRegistryError([RegistryIssue(name, "oversized", "registry exceeds 64 KiB")])
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise KnowledgeRegistryError([RegistryIssue(name, "unreadable", str(error))]) from error
    return validate_registry(document, repository)


def find_source(registry: KnowledgeRegistry, source_id: str) -> KnowledgeSource:
    for source in registry.sources:
        if source.id == source_id:
            return source
    raise KnowledgeRegistryError(
        [RegistryIssue("source", "unknown_source", f"no source with ID '{source_id}'")]
    )
