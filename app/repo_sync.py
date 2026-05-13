"""Synchronize an on-disk git checkout with its remote and agent metadata.

Safe to run repeatedly: ``fetch`` + fast-forward only (no automerge of
divergent work). Merge conflicts and non-FF history surface as
:class:`RepoSyncError` (logged, ``last_synced_at`` is not updated).
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from app.db.dynamodb import now_iso
from app.logging import get_logger, log_context, log_event
from app import repo_initializer

log = get_logger(__name__)

_CRED = re.compile(r"https?://[^/\s:]+:[^/\s@]+@")


def _scrub(s: str) -> str:
    return _CRED.sub("https://***@", s)


@dataclass
class _GitResult:
    returncode: int
    stdout: str
    stderr: str


class RepoSyncError(Exception):
    """The repository could not be fast-forwarded or a git step failed."""


def _is_git_work_tree(path: Path) -> bool:
    return (path / ".git").is_dir() or (path / ".git").is_file()


async def _run_git(
    repo: Path,
    args: Sequence[str],
    *,
    timeout: float = 300.0,
) -> _GitResult:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/true",
        "LC_ALL": "C",
    }
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RepoSyncError(f"git {args[0]!r} timed out after {timeout:.0f}s") from exc
    return _GitResult(
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _default_branch_name(repo_path: Path) -> str:
    meta = repo_initializer.read_metadata(repo_path)
    if not meta:
        return "main"
    b = str(meta.get("default_branch") or "main").strip()
    return b or "main"


async def _has_origin(repo: Path) -> bool:
    r = await _run_git(repo, ["remote", "get-url", "origin"], timeout=15.0)
    return r.returncode == 0


async def _current_branch_name(repo: Path) -> str:
    r = await _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=15.0)
    if r.returncode != 0:
        return "HEAD"
    return (r.stdout.strip() or "HEAD") or "HEAD"


async def _origin_has_ref(repo: Path, branch: str) -> bool:
    r = await _run_git(
        repo, ["rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
        timeout=15.0,
    )
    if r.returncode == 0:
        return True
    r2 = await _run_git(repo, ["ls-remote", "--heads", "origin", branch], timeout=60.0)
    return r2.returncode == 0 and bool(r2.stdout.strip())


def _is_ff_or_merge_error(stderr: str, stdout: str) -> bool:
    t = f"{stderr}\n{stdout}".lower()
    if "conflict" in t:
        return True
    if "not something we can merge" in t:
        return True
    if "non-fast-forward" in t and "ff" in t:
        return True
    if "ff-only" in t:
        return True
    if "diverg" in t or "diverging" in t:
        return True
    return False


async def sync_repo(repo_path: str) -> None:
    """``git fetch`` and fast-forward to match ``origin/<default_branch>``.

    Reads **default_branch** from ``.agent/metadata.json`` (else ``"main"``).
    If HEAD is **detached** or the current branch is not the default, runs
    ``git checkout -B <default> origin/<default>`` and ``git reset --hard
    origin/<default>`` so the worktree matches the remote default tip, then
    ``git merge --ff-only origin/<default>`` (usually no-op) to align with
    fetches. Sets **last_synced_at** in metadata when everything succeeds.

    On merge conflict or a non-FF state, logs an error and raises
    :class:`RepoSyncError` without writing ``last_synced_at``.

    Idempotent: repeated calls are safe; nothing to do yields an up-to-date
    merge result.
    """
    path = Path(repo_path).expanduser().resolve()
    with log_context(dest=str(path)):
        if not path.is_dir():
            raise RepoSyncError(f"not a directory: {path}")
        if not _is_git_work_tree(path):
            raise RepoSyncError(f"not a git work tree: {path}")

        default_branch = _default_branch_name(path)
        log_event(
            log, "repo_sync.start",
            "sync",
            default_branch=default_branch,
        )

        if not await _has_origin(path):
            raise RepoSyncError("no remote 'origin' configured")

        fetch = await _run_git(
            path, ["fetch", "origin", "--prune", "--recurse-submodules=no"],
            timeout=300.0,
        )
        if fetch.returncode != 0:
            raise RepoSyncError(f"git fetch failed: {_scrub(fetch.stderr)}")

        if not await _origin_has_ref(path, default_branch):
            log.error("repo_sync: no origin/%s after fetch", default_branch)
            raise RepoSyncError(
                f"Remote has no {default_branch!r}; set default_branch in .agent/metadata.json"
            )

        current = await _current_branch_name(path)
        on_default = current == default_branch and current != "HEAD"
        if not on_default or current == "HEAD":
            if current == "HEAD":
                log.info("repo_sync: detached HEAD, attaching to %r", default_branch)
            else:
                log.info(
                    "repo_sync: branch mismatch %r != %r; resetting to origin",
                    current,
                    default_branch,
                )
            co = await _run_git(
                path,
                ["checkout", "-B", default_branch, f"origin/{default_branch}"],
                timeout=120.0,
            )
            if co.returncode != 0:
                raise RepoSyncError(
                    f"git checkout to {default_branch!r} failed: {_scrub(co.stderr)}"
                )
            rh = await _run_git(
                path, ["reset", "--hard", f"origin/{default_branch}"], timeout=60.0
            )
            if rh.returncode != 0:
                raise RepoSyncError(
                    f"git reset --hard failed: {_scrub(rh.stderr)}"
                )
        m = await _run_git(
            path, ["merge", "--ff-only", f"origin/{default_branch}"], timeout=120.0
        )
        if m.returncode != 0:
            if _is_ff_or_merge_error(m.stderr, m.stdout):
                log.error(
                    "repo_sync: fast-forward/merge error (resolve manually): %s",
                    _scrub(m.stderr)[:2000],
                )
            else:
                log.error("repo_sync: merge --ff-only failed: %s", _scrub(m.stderr)[:2000])
            raise RepoSyncError(
                f"Cannot fast-forward: {_scrub(m.stderr).strip() or m.stdout.strip()!r}"
            )

        if not repo_initializer.merge_metadata(
            path, {"last_synced_at": now_iso()}
        ):
            log.warning("repo_sync: did not update metadata (no .agent/metadata.json?)")

        log_event(log, "repo_sync.done", "synced", default_branch=default_branch)


__all__ = [
    "RepoSyncError",
    "sync_repo",
]
