"""Dedicated Git worktrees and post-run path-scope evidence."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


class WorktreeError(ValueError):
    """A target or proposed worktree is unsafe or unavailable."""


@dataclass(frozen=True)
class WorktreeInfo:
    target_repository: Path
    worktree_path: Path
    branch: str
    base_commit: str


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorktreeError(f"Git unavailable: {error}") from error
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise WorktreeError(f"Git operation failed: {message[:500]}")
    return result.stdout


def inspect_repository(path: Path) -> tuple[Path, str]:
    """Require an exact primary checkout root and capture its HEAD commit."""
    repo = path.resolve(strict=True)
    root = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != repo or not (repo / ".git").is_dir():
        raise WorktreeError(
            "target must be the primary Git checkout root, not a linked worktree/subdirectory"
        )
    metadata = repo / ".patchfleet"
    if metadata.is_symlink() or _git(repo, "ls-files", "--", ".patchfleet").strip():
        raise WorktreeError(
            ".patchfleet must be an untracked, non-symlink local metadata directory"
        )
    base = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", base) is None:
        raise WorktreeError("target repository has no valid HEAD commit")
    return repo, base


def ensure_local_metadata(repo: Path) -> None:
    """Create an ignored local metadata directory without altering tracked files."""
    metadata = repo / ".patchfleet"
    if metadata.is_symlink():
        raise WorktreeError(".patchfleet must not be a symlink")
    metadata.mkdir(exist_ok=True)
    ignore_file = metadata / ".gitignore"
    if not ignore_file.exists():
        ignore_file.write_text("*\n", encoding="utf-8")
    check = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", ".patchfleet/worktrees/probe"],
        check=False,
        capture_output=True,
        timeout=15,
    )
    if check.returncode != 0:
        raise WorktreeError(".patchfleet is not ignored; add an ignore rule before execution")


def _safe_name(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9-]", "-", value).strip("-")[:48] or "task"
    return f"{slug}-{sha256(value.encode()).hexdigest()[:10]}"


def provision(repo: Path, run_id: str, task_id: str, base_commit: str) -> WorktreeInfo:
    """Create one branch/worktree; never remove it automatically."""
    root, current_base = inspect_repository(repo)
    if current_base != base_commit:
        raise WorktreeError(
            "target HEAD changed since run creation; explicit replanning is required"
        )
    worktree = root / ".patchfleet" / "worktrees" / _safe_name(run_id) / _safe_name(task_id)
    branch = f"patchfleet/{_safe_name(run_id)}/{_safe_name(task_id)}"
    if worktree.exists() or worktree.is_symlink():
        raise WorktreeError(f"worktree path already exists: {worktree}")
    for component in (root / ".patchfleet" / "worktrees", worktree.parent):
        if component.is_symlink():
            raise WorktreeError(f"worktree parent must not be a symlink: {component}")
    ensure_local_metadata(root)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if root not in worktree.parent.resolve().parents:
        raise WorktreeError("worktree parent resolves outside the target repository")
    _git(root, "worktree", "add", "-b", branch, str(worktree), base_commit)
    resolved = worktree.resolve()
    if resolved == root or root not in resolved.parents:
        raise WorktreeError("worktree location is unsafe")
    return WorktreeInfo(root, resolved, branch, base_commit)


def changed_paths(info: WorktreeInfo) -> tuple[str, ...]:
    """Include committed, staged, unstaged, and untracked changes."""
    tracked = _git(
        info.worktree_path, "diff", "--name-only", "--no-renames", "-z", info.base_commit
    )
    untracked = _git(info.worktree_path, "ls-files", "--others", "-z")
    return tuple(
        sorted(set((tracked + untracked).decode("utf-8", "replace").strip("\0").split("\0")) - {""})
    )


def primary_snapshot(repo: Path) -> tuple[str, bytes]:
    """Read primary HEAD and status to detect accidental out-of-worktree writes."""
    head = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    status = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    return head, status


def outside_scope(paths: tuple[str, ...], allowed_paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        path
        for path in paths
        if not any(
            path == allowed or path.startswith(allowed.rstrip("/") + "/")
            for allowed in allowed_paths
        )
    )
