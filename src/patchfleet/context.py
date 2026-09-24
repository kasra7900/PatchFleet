"""Bounded, structural evidence from tracked files in a local Git repository."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import tomllib
from collections import Counter
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import Field

from .contracts import Fingerprint, StrictModel
from .worktrees import ensure_local_metadata, inspect_repository

MAX_FILE_BYTES = 65536
MAX_EVIDENCE_ITEMS = 300
SENSITIVE_PARTS = {
    ".env",
    "credentials",
    "secrets",
    "secret",
    "tokens",
    "token",
    "private",
    "id_rsa",
    "id_ed25519",
    ".patchfleet",
    ".git",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".aws",
    ".ssh",
    ".docker",
    ".kube",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".sqlite3"}
MANIFESTS = {
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
}
TEST_CONFIGS = {"pytest.ini", "tox.ini", "ruff.toml", "mypy.ini", ".pre-commit-config.yaml"}


class ContextError(ValueError):
    """Repository evidence cannot be collected or trusted."""


class EvidenceLocator(StrictModel):
    path: str
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    section_heading: str | None = None


class RepositoryEvidence(StrictModel):
    evidence_id: str
    kind: Literal[
        "manifest",
        "test_config",
        "documentation",
        "contribution_rule",
        "public_interface",
        "selected_module",
    ]
    locator: EvidenceLocator
    summary: str
    content_sha256: Fingerprint


class RepositoryContext(StrictModel):
    schema_version: Literal["0.1"]
    context_id: Fingerprint
    repository: str
    base_commit: str
    tracked_file_count: int = Field(ge=0)
    tree_summary: dict[str, int]
    selected_paths: tuple[str, ...]
    truncated: bool
    evidence: tuple[RepositoryEvidence, ...]


def _git_files(repository: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-z"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if result.returncode:
        raise ContextError("could not enumerate tracked Git files")
    if not result.stdout:
        return ()
    return tuple(sorted(result.stdout.decode("utf-8", "replace").strip("\0").split("\0")))


def _excluded(path: str) -> bool:
    parts = PurePosixPath(path).parts
    lowered = tuple(part.casefold() for part in parts)
    name = lowered[-1] if lowered else ""
    return (
        not parts
        or any(part in SENSITIVE_PARTS for part in lowered)
        or name.startswith(".env")
        or any(
            word in name
            for word in ("credential", "secret", "private_key", "api_key", "password", "token")
        )
        or re.search(r"(?i)(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|[A-Za-z0-9_-]{40,})", name)
        is not None
        or PurePosixPath(name).suffix in SENSITIVE_SUFFIXES
    )


def _safe_label(value: str, limit: int = 120) -> str:
    """Keep only structural labels, never arbitrary source or configuration values."""
    if re.search(
        r"(?i)(password|secret|token|credential|api.?key|sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|[A-Za-z0-9_+/-]{40,})",
        value,
    ):
        return "[redacted]"
    return re.sub(r"[^A-Za-z0-9 _./:@+()-]", "", value).strip()[:limit]


def _file_bytes(repository: Path, path: str) -> bytes | None:
    target = repository / path
    if ".." in PurePosixPath(path).parts:
        return None
    root = repository.resolve()
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return None
    if root not in resolved.parents:
        return None
    if target.is_symlink() or any(
        parent.is_symlink() for parent in target.parents if root in parent.parents
    ):
        return None
    if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
        return None
    data = target.read_bytes()
    if b"\0" in data:
        return None
    try:
        data.decode("utf-8")
    except UnicodeError:
        return None
    return data


def _kind(path: str, selected: set[str]) -> str | None:
    name = PurePosixPath(path).name
    lower = path.casefold()
    if name in MANIFESTS:
        return "manifest"
    if lower.endswith(".md") and (
        lower.startswith("docs/") or name.upper().startswith(("README", "CONTRIBUTING", "AGENTS"))
    ):
        return "documentation"
    if path in selected:
        return "selected_module"
    if name in TEST_CONFIGS or lower.startswith(".github/workflows/") or lower.startswith("tests/"):
        return "test_config"
    if PurePosixPath(lower).suffix in {".py", ".js", ".ts", ".tsx", ".go", ".rs"} and (
        lower.startswith("src/") or name == "__init__.py"
    ):
        return "public_interface"
    return None


def _manifest_summary(path: str, text: str) -> str:
    name = PurePosixPath(path).name
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
            sections = ", ".join(sorted(_safe_label(key) for key in data if isinstance(key, str)))
            project = data.get("project", {})
            python = project.get("requires-python", "") if isinstance(project, dict) else ""
            safe_python = (
                python
                if isinstance(python, str) and re.fullmatch(r"[<>=!~0-9., *]+", python)
                else ""
            )
            dependencies = project.get("dependencies", ()) if isinstance(project, dict) else ()
            names = (
                [
                    _safe_label(match.group(0))
                    for dependency in dependencies[:20]
                    if isinstance(dependency, str)
                    if (match := re.match(r"[A-Za-z0-9_.-]+", dependency))
                ]
                if isinstance(dependencies, list | tuple)
                else []
            )
            return (
                f"Python manifest; sections: {sections}; requires-python: {safe_python}; "
                f"dependency names: {', '.join(names)}"
            )
        except (ValueError, TypeError):
            return "Python manifest present; parsing unavailable"
    if name == "package.json":
        try:
            data = json.loads(text)
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            names = (
                ", ".join(sorted(_safe_label(key) for key in scripts))
                if isinstance(scripts, dict)
                else ""
            )
            dependencies = data.get("dependencies", {}) if isinstance(data, dict) else {}
            packages = (
                ", ".join(sorted(_safe_label(key) for key in dependencies)[:20])
                if isinstance(dependencies, dict)
                else ""
            )
            return f"JavaScript manifest; script names: {names}; dependency names: {packages}"
        except (ValueError, TypeError):
            return "JavaScript manifest present; parsing unavailable"
    return f"Tracked package manifest: {_safe_label(name)}"


def _evidence_for_file(path: str, data: bytes, kind: str) -> list[RepositoryEvidence]:
    text = data.decode("utf-8")
    lines = text.splitlines()
    digest = sha256(data).hexdigest()
    findings: list[tuple[int, int, str, str | None, str]] = []
    if kind == "manifest":
        findings.append((1, max(1, len(lines)), _manifest_summary(path, text), None, kind))
    elif kind == "test_config":
        summary = f"Tracked test or CI configuration: {_safe_label(path)}"
        if path.startswith(".github/workflows/") and path.endswith((".yaml", ".yml")):
            try:
                workflow = yaml.safe_load(text)
                jobs = workflow.get("jobs", {}) if isinstance(workflow, dict) else {}
                if isinstance(jobs, dict):
                    summary += "; job names: " + ", ".join(
                        sorted(_safe_label(key) for key in jobs)[:20]
                    )
            except yaml.YAMLError:
                pass
        findings.append((1, max(1, len(lines)), summary, None, kind))
    elif kind == "documentation":
        for number, line in enumerate(lines, 1):
            if match := re.match(r"^#{1,6}\s+(.+)$", line):
                heading = _safe_label(match.group(1))
                if heading:
                    findings.append(
                        (number, number, f"Documentation section: {heading}", heading, kind)
                    )
            elif PurePosixPath(path).name.upper().startswith("CONTRIBUTING") and re.match(
                r"^[-*]\s+", line
            ):
                rule = line[2:].strip()
                if len(rule) <= 180 and not re.search(
                    r"(?i)(password|secret|token|credential|api.?key|sk-)", rule
                ):
                    safe = _safe_label(rule, 180)
                    if safe:
                        findings.append(
                            (
                                number,
                                number,
                                f"Contribution rule: {safe}",
                                None,
                                "contribution_rule",
                            )
                        )
        if not findings:
            findings.append(
                (1, 1, f"Tracked project documentation: {_safe_label(path)}", None, kind)
            )
    elif kind in {"public_interface", "selected_module"} and path.endswith(".py"):
        try:
            module = ast.parse(text)
            names = [
                node
                for node in module.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and not node.name.startswith("_")
            ]
        except SyntaxError:
            names = []
        for node in names[:30]:
            findings.append(
                (
                    node.lineno,
                    node.lineno,
                    f"Public {type(node).__name__}: {_safe_label(node.name)}",
                    None,
                    kind,
                )
            )
        if not names:
            findings.append((1, 1, f"Tracked source module: {_safe_label(path)}", None, kind))
    elif kind in {"public_interface", "selected_module"} and PurePosixPath(path).suffix in {
        ".js",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
    }:
        suffix = PurePosixPath(path).suffix
        expression = {
            ".js": r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+([A-Za-z_$][\w$]*)",
            ".ts": r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+([A-Za-z_$][\w$]*)",
            ".tsx": r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const)\s+([A-Za-z_$][\w$]*)",
            ".go": r"^func\s+([A-Z][A-Za-z0-9_]*)\s*\(",
            ".rs": r"^\s*pub\s+(?:struct|enum|trait|fn)\s+([A-Za-z_][A-Za-z0-9_]*)",
        }[suffix]
        for number, line in enumerate(lines, 1):
            if match := re.match(expression, line):
                findings.append(
                    (number, number, f"Public symbol: {_safe_label(match.group(1))}", None, kind)
                )
            if len(findings) >= 30:
                break
        if not findings:
            findings.append((1, 1, f"Tracked source module: {_safe_label(path)}", None, kind))
    elif kind == "selected_module":
        findings.append((1, 1, f"Selected tracked file: {_safe_label(path)}", None, kind))
    return [
        RepositoryEvidence(
            evidence_id="repo:"
            + sha256(f"{path}:{start}:{end}:{summary}:{digest}".encode()).hexdigest()[:20],
            kind=actual_kind,
            locator=EvidenceLocator(
                path=path, start_line=start, end_line=end, section_heading=heading
            ),
            summary=summary,
            content_sha256=digest,
        )
        for start, end, summary, heading, actual_kind in findings
    ]


def _tree_summary(paths: tuple[str, ...]) -> dict[str, int]:
    suffixes = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".rs": "rust",
        ".go": "go",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".toml": "toml",
    }
    counts: Counter[str] = Counter()
    known_directories = {"src", "tests", "docs", ".github", "app", "lib"}
    for path in paths:
        counts["language:" + suffixes.get(PurePosixPath(path).suffix.casefold(), "other")] += 1
        first = PurePosixPath(path).parts[0]
        counts["directory:" + (first if first in known_directories else "root-or-other")] += 1
    return dict(sorted(counts.items()))


def _context_id(data: dict[str, object]) -> str:
    return sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def inspect_context(repository: Path, selected_paths: tuple[str, ...] = ()) -> RepositoryContext:
    """Inspect tracked, small, non-secret files without changing the Git checkout."""
    root, base = inspect_repository(repository)
    tracked = _git_files(root)
    safe = tuple(path for path in tracked if not _excluded(path))
    selected = set(selected_paths)
    for path in selected:
        if path not in safe or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ContextError(f"selected path must be a safe tracked file: {path}")
        if _file_bytes(root, path) is None:
            raise ContextError(f"selected path is binary, oversized, or a symlink: {path}")
    evidence: list[RepositoryEvidence] = []
    truncated = False
    ordered_paths = tuple(sorted(selected)) + tuple(path for path in safe if path not in selected)
    for path in ordered_paths:
        kind = _kind(path, selected)
        if kind is None:
            continue
        data = _file_bytes(root, path)
        if data is None:
            continue
        for item in _evidence_for_file(path, data, kind):
            if len(evidence) >= MAX_EVIDENCE_ITEMS:
                truncated = True
                break
            evidence.append(item)
        if truncated:
            break
    content: dict[str, object] = {
        "schema_version": "0.1",
        "repository": str(root),
        "base_commit": base,
        "tracked_file_count": len(tracked),
        "tree_summary": _tree_summary(safe),
        "selected_paths": tuple(sorted(selected)),
        "truncated": truncated,
        "evidence": tuple(item.model_dump(mode="json") for item in evidence),
    }
    return RepositoryContext.model_validate({**content, "context_id": _context_id(content)})


def context_path(repository: Path, context_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", context_id) is None:
        raise ContextError("context ID must be a SHA-256 digest")
    return repository / ".patchfleet" / "planning" / "contexts" / f"{context_id}.json"


def persist_context(context: RepositoryContext) -> Path:
    root = Path(context.repository)
    ensure_local_metadata(root)
    path = context_path(root, context.context_id)
    if path.parent.is_symlink() or path.parent.parent.is_symlink():
        raise ContextError("planning context directory must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(context.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    if path.is_symlink():
        raise ContextError("context artifact must not be a symlink")
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ContextError("stored context ID conflicts with different content")
    else:
        path.write_text(encoded, encoding="utf-8")
    return path


def load_context(repository: Path, context_id: str) -> RepositoryContext:
    root, _ = inspect_repository(repository)
    path = context_path(root, context_id)
    if (
        path.parent.is_symlink()
        or path.parent.parent.is_symlink()
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ContextError(f"unknown context ID: {context_id}")
    try:
        context = RepositoryContext.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContextError(f"invalid stored context: {error}") from error
    content = context.model_dump(mode="json", exclude={"context_id"})
    if context.context_id != context_id or _context_id(content) != context_id:
        raise ContextError("stored context identity does not match its content")
    if context.repository != str(root):
        raise ContextError("context belongs to a different repository")
    return context


def evidence_is_current(repository: Path, item: RepositoryEvidence) -> bool:
    if _excluded(item.locator.path):
        return False
    data = _file_bytes(repository, item.locator.path)
    return data is not None and sha256(data).hexdigest() == item.content_sha256
