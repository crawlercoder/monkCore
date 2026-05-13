"""Deterministic repository synchronization.

Foundation for continuously-updated repository intelligence: given a
``repo_id``, look up its registry row, validate the local clone, fetch
the configured remote branch, compare SHAs, enumerate changed files
with **machine-parseable** git plumbing, and fast-forward the work tree
when safe. Pure synchronous code; no queues, no workers, no re-indexing.

Result shape
------------
Every code path returns a ``dict``. Callers should branch on the boolean
``ok`` field first, then on ``updated`` for success cases::

    {"ok": True, "updated": False, "local_sha": "...", "remote_sha": "..."}
    {"ok": True, "updated": True, "old_sha": "...", "new_sha": "...",
     "changed_files": [...], "deleted_files": [...], "renamed_files": [...]}
    {"ok": False, "error": {"code": "...", "message": "...", "details": {...}}}

Design notes
------------
* Git commands run via :mod:`subprocess` with explicit ``timeout=`` and an
  environment that disables prompts and pins locale (``LC_ALL=C``) — so we
  never block on a TTY and parsers stay stable across machines.
* File change detection uses ``git diff --name-status -z`` (NUL-delimited,
  rename/copy aware) — no natural-language stderr parsing anywhere.
* The pull step is **fast-forward only** (``git merge --ff-only``); a
  dirty work tree or a divergent history is reported as a structured
  error rather than reset or force-pulled.
* Registry lookups go through :class:`app.db.repos.ReposRepository` so the
  same call works for both DynamoDB and the in-memory dev backend.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import repo_initializer, storage_manager
from app.db.repos import ReposRepository
from app.logging import get_logger, log_context, log_event
from app.models.repos import Repo

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Public exception                                                            #
# --------------------------------------------------------------------------- #


class RepoSyncError(Exception):
    """Raised for programmer-facing misuse (e.g. running inside an event loop).

    Operational failures (missing metadata, dirty work tree, fetch errors)
    do **not** raise — they return a structured error dict so callers can
    serialise the outcome directly.
    """


# --------------------------------------------------------------------------- #
# Error codes                                                                 #
# --------------------------------------------------------------------------- #


class SyncErrorCode(str, Enum):
    """Stable error codes returned in the ``error.code`` field."""

    INVALID_INPUT = "invalid_input"
    REPO_NOT_FOUND = "repo_not_found"
    REPO_METADATA_INVALID = "repo_metadata_invalid"
    LOCAL_PATH_MISSING = "local_path_missing"
    NOT_A_GIT_REPO = "not_a_git_repo"
    GIT_NOT_INSTALLED = "git_not_installed"
    GIT_TIMEOUT = "git_timeout"
    GIT_COMMAND_FAILED = "git_command_failed"
    REMOTE_BRANCH_MISSING = "remote_branch_missing"
    DIRTY_WORK_TREE = "dirty_work_tree"
    NON_FAST_FORWARD = "non_fast_forward"


# --------------------------------------------------------------------------- #
# Tuning                                                                      #
# --------------------------------------------------------------------------- #


_DEFAULT_FETCH_TIMEOUT_S: float = 300.0
_DEFAULT_FAST_TIMEOUT_S: float = 30.0
_DEFAULT_MERGE_TIMEOUT_S: float = 120.0


# --------------------------------------------------------------------------- #
# Git subprocess primitives                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _GitResult:
    """Captured output of a single ``git`` invocation."""

    returncode: int
    stdout: str
    stderr: str


def _git_env() -> Dict[str, str]:
    """Return a hardened environment for ``git``.

    * ``GIT_TERMINAL_PROMPT=0`` / ``GIT_ASKPASS=/bin/true`` — never prompt.
    * ``LC_ALL=C`` — deterministic, English plumbing output.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"
    env["LC_ALL"] = "C"
    return env


def _run_git(
    repo_path: Path,
    args: Sequence[str],
    *,
    timeout: float,
) -> _GitResult:
    """Run ``git <args>`` inside ``repo_path`` and capture text output.

    Raises :class:`subprocess.TimeoutExpired` on timeout (propagated to
    the caller, which converts it to a structured error). All other
    failures (non-zero exit, missing binary) are reported via the return
    value or :class:`FileNotFoundError`.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return _GitResult(
        returncode=int(proc.returncode or 0),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _git_or_error(
    repo_path: Path,
    args: Sequence[str],
    *,
    timeout: float,
    op_label: str,
) -> Tuple[Optional[_GitResult], Optional[Dict[str, Any]]]:
    """Run ``git`` and translate exceptions into structured errors.

    Returns ``(result, None)`` on a successful invocation (regardless of
    exit code; the caller inspects ``returncode``), or ``(None, error)``
    when the subprocess could not be executed / timed out.
    """
    try:
        return _run_git(repo_path, args, timeout=timeout), None
    except FileNotFoundError as exc:
        return None, _err(
            SyncErrorCode.GIT_NOT_INSTALLED,
            f"'git' executable not found on PATH",
            details={"op": op_label, "system_error": str(exc)},
        )
    except subprocess.TimeoutExpired as exc:
        return None, _err(
            SyncErrorCode.GIT_TIMEOUT,
            f"git {op_label!r} timed out after {timeout:.0f}s",
            details={"op": op_label, "timeout_seconds": timeout},
        )


# --------------------------------------------------------------------------- #
# Deterministic diff parsing                                                  #
# --------------------------------------------------------------------------- #


def _parse_name_status_z(stream: str) -> Tuple[List[str], List[str], List[Dict[str, str]]]:
    """Parse ``git diff --name-status -z`` output.

    Format (NUL-delimited):

    * non-rename: ``<status>\\0<path>\\0`` — status is ``A``/``M``/``D``/``T``/``U``
    * rename/copy: ``<status>\\0<old_path>\\0<new_path>\\0`` — status starts
      with ``R`` or ``C`` and may be followed by a similarity number
      (``R100``).

    Returns three lists:

    * ``changed_files`` — added, modified, type-changed, copied (new path)
    * ``deleted_files`` — paths removed in the new tree
    * ``renamed_files`` — ``[{from, to, similarity?}]`` entries
    """
    if not stream:
        return [], [], []

    tokens = stream.split("\x00")
    if tokens and tokens[-1] == "":
        tokens.pop()

    changed: List[str] = []
    deleted: List[str] = []
    renamed: List[Dict[str, str]] = []

    i = 0
    n = len(tokens)
    while i < n:
        status = tokens[i]
        i += 1
        if not status:
            continue
        head = status[0]
        if head in ("R", "C"):
            if i + 1 >= n:
                break
            old_path = tokens[i]
            new_path = tokens[i + 1]
            i += 2
            entry: Dict[str, str] = {"from": old_path, "to": new_path}
            similarity = status[1:].strip()
            if similarity.isdigit():
                entry["similarity"] = similarity
            if head == "R":
                renamed.append(entry)
            else:
                changed.append(new_path)
        else:
            if i >= n:
                break
            path = tokens[i]
            i += 1
            if head == "D":
                deleted.append(path)
            else:
                changed.append(path)

    return changed, deleted, renamed


# --------------------------------------------------------------------------- #
# Registry lookup                                                             #
# --------------------------------------------------------------------------- #


def _load_repo_row(repo_id: str) -> Optional[Repo]:
    """Resolve ``repo_id`` against the registry (DynamoDB or memory).

    Wraps the existing async :class:`ReposRepository` in a clean
    synchronous call. Raises :class:`RepoSyncError` if invoked from a
    running event loop — :func:`asyncio.run` is not re-entrant.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ReposRepository().get(repo_id))
    raise RepoSyncError(
        "sync_repository() is synchronous and cannot be called from a running event loop; "
        "wrap it with asyncio.to_thread(...)"
    )


def _resolve_metadata(repo_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return ``(metadata, error)`` for the registry row.

    The ``metadata`` dict has the canonical shape used downstream:
    ``{repo_id, org_id, repo_url, tracked_branch, local_repo_path}``.
    """
    try:
        repo = _load_repo_row(repo_id)
    except RepoSyncError:
        raise
    except Exception as exc:
        return None, _err(
            SyncErrorCode.REPO_METADATA_INVALID,
            f"registry lookup failed for repo_id={repo_id!r}: {exc}",
            details={"exception_type": type(exc).__name__},
        )

    if repo is None:
        return None, _err(
            SyncErrorCode.REPO_NOT_FOUND,
            f"no registry row for repo_id={repo_id!r}",
        )

    try:
        local_repo_path = storage_manager.get_repo_path(repo.org_id, repo.repo_id)
    except ValueError as exc:
        return None, _err(
            SyncErrorCode.REPO_METADATA_INVALID,
            f"invalid org_id/repo_id for storage path: {exc}",
            details={"org_id": repo.org_id, "repo_id": repo.repo_id},
        )

    return (
        {
            "repo_id": repo.repo_id,
            "org_id": repo.org_id,
            "repo_url": repo.repo_url,
            "tracked_branch": repo.branch,
            "local_repo_path": local_repo_path,
        },
        None,
    )


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #


def _validate_local_checkout(local_repo_path: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """Confirm the local clone is on disk and looks like a git work tree."""
    path = Path(local_repo_path).expanduser()
    if not path.is_dir():
        return None, _err(
            SyncErrorCode.LOCAL_PATH_MISSING,
            f"local clone directory does not exist: {path}",
            details={"local_repo_path": str(path)},
        )
    dot_git = path / ".git"
    if not (dot_git.is_dir() or dot_git.is_file()):
        return None, _err(
            SyncErrorCode.NOT_A_GIT_REPO,
            f"no .git found under {path}",
            details={"local_repo_path": str(path)},
        )
    return path.resolve(), None


# --------------------------------------------------------------------------- #
# Helper: structured error builder                                            #
# --------------------------------------------------------------------------- #


def _err(
    code: SyncErrorCode,
    message: str,
    *,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the standard ``error`` envelope."""
    err: Dict[str, Any] = {"code": code.value, "message": message}
    if details:
        err["details"] = details
    return err


def _fail(
    code: SyncErrorCode,
    message: str,
    *,
    details: Optional[Dict[str, Any]] = None,
    repo_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a structured failure dict and log a single warning."""
    error = _err(code, message, details=details)
    log_event(
        log,
        "repo_sync.failed",
        message,
        level=30,  # logging.WARNING
        code=code.value,
        **({"details": details} if details else {}),
    )
    out: Dict[str, Any] = {"ok": False, "updated": False, "error": error}
    if repo_meta is not None:
        out["repo"] = {
            "repo_id": repo_meta.get("repo_id"),
            "org_id": repo_meta.get("org_id"),
            "tracked_branch": repo_meta.get("tracked_branch"),
        }
    return out


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #


def sync_repository(repo_id: str) -> Dict[str, Any]:
    """Synchronize the local clone of ``repo_id`` with its tracked remote branch.

    Steps (each step short-circuits to a structured error on failure):

    1. Load registry metadata (``repo_id``, ``org_id``, ``repo_url``,
       ``tracked_branch``, ``local_repo_path``).
    2. Validate the local checkout (directory exists, ``.git`` present).
    3. ``git fetch origin`` with timeout.
    4. Compare ``HEAD`` SHA against ``origin/<tracked_branch>``.
    5. If different — enumerate added/modified/deleted/renamed files via
       ``git diff --name-status -z``.
    6. Fast-forward the work tree (``git merge --ff-only``) — refuses to
       run when the working tree is dirty or the history is divergent.
    7. Return a structured result.

    Never raises for expected operational failures; see :class:`SyncErrorCode`
    for the catalogue of return codes.
    """
    if not isinstance(repo_id, str) or not repo_id.strip():
        return _fail(SyncErrorCode.INVALID_INPUT, "repo_id must be a non-empty string")
    repo_id = repo_id.strip()

    with log_context(repo_id=repo_id):
        log_event(log, "repo_sync.start", "sync_repository: begin")

        meta, err = _resolve_metadata(repo_id)
        if err:
            return _fail(
                SyncErrorCode(err["code"]),
                err["message"],
                details=err.get("details"),
            )
        assert meta is not None

        with log_context(org_id=meta["org_id"], tracked_branch=meta["tracked_branch"]):
            return _sync_with_metadata(meta)


def _sync_with_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Pipeline body once metadata has been resolved (kept small on purpose)."""
    tracked_branch = meta["tracked_branch"]

    repo_path, err = _validate_local_checkout(meta["local_repo_path"])
    if err:
        return _fail(
            SyncErrorCode(err["code"]),
            err["message"],
            details=err.get("details"),
            repo_meta=meta,
        )
    assert repo_path is not None

    fetch_err = _fetch_origin(repo_path)
    if fetch_err is not None:
        return _fail(
            SyncErrorCode(fetch_err["code"]),
            fetch_err["message"],
            details=fetch_err.get("details"),
            repo_meta=meta,
        )

    local_sha, err = _rev_parse(repo_path, "HEAD")
    if err:
        return _fail(
            SyncErrorCode(err["code"]),
            err["message"],
            details=err.get("details"),
            repo_meta=meta,
        )

    remote_ref = f"refs/remotes/origin/{tracked_branch}"
    remote_sha, err = _rev_parse(repo_path, remote_ref)
    if err:
        # rev-parse on a missing ref returns a non-zero exit; map that to a
        # specific code so callers can show "did you create the branch upstream?"
        return _fail(
            SyncErrorCode.REMOTE_BRANCH_MISSING,
            f"remote branch not found after fetch: origin/{tracked_branch}",
            details={"ref": remote_ref, "rev_parse_stderr": err.get("details", {}).get("stderr", "")[:500]},
            repo_meta=meta,
        )

    assert local_sha is not None and remote_sha is not None

    if local_sha == remote_sha:
        log_event(
            log,
            "repo_sync.up_to_date",
            "sync_repository: up to date",
            local_sha=local_sha,
        )
        return {
            "ok": True,
            "updated": False,
            "repo_id": meta["repo_id"],
            "org_id": meta["org_id"],
            "tracked_branch": tracked_branch,
            "local_sha": local_sha,
            "remote_sha": remote_sha,
        }

    changed, deleted, renamed, err = _detect_changes(repo_path, local_sha, remote_sha)
    if err:
        return _fail(
            SyncErrorCode(err["code"]),
            err["message"],
            details=err.get("details"),
            repo_meta=meta,
        )

    clean_err = _ensure_clean_work_tree(repo_path)
    if clean_err is not None:
        return _fail(
            SyncErrorCode(clean_err["code"]),
            clean_err["message"],
            details=clean_err.get("details"),
            repo_meta=meta,
        )

    ff_err = _fast_forward_merge(repo_path, tracked_branch)
    if ff_err is not None:
        return _fail(
            SyncErrorCode(ff_err["code"]),
            ff_err["message"],
            details=ff_err.get("details"),
            repo_meta=meta,
        )

    new_sha, err = _rev_parse(repo_path, "HEAD")
    if err or new_sha is None:
        return _fail(
            SyncErrorCode.GIT_COMMAND_FAILED,
            "could not resolve HEAD after fast-forward",
            details=(err or {}).get("details"),
            repo_meta=meta,
        )

    try:
        repo_initializer.merge_metadata(repo_path, {"last_synced_sha": new_sha})
    except Exception as exc:
        log.warning("repo_sync: best-effort metadata update failed: %s", exc)

    log_event(
        log,
        "repo_sync.updated",
        "sync_repository: fast-forwarded",
        old_sha=local_sha,
        new_sha=new_sha,
        changed=len(changed),
        deleted=len(deleted),
        renamed=len(renamed),
    )
    return {
        "ok": True,
        "updated": True,
        "repo_id": meta["repo_id"],
        "org_id": meta["org_id"],
        "tracked_branch": tracked_branch,
        "old_sha": local_sha,
        "new_sha": new_sha,
        "changed_files": changed,
        "deleted_files": deleted,
        "renamed_files": renamed,
    }


# --------------------------------------------------------------------------- #
# Step helpers (small, typed, single-purpose)                                 #
# --------------------------------------------------------------------------- #


def _fetch_origin(repo_path: Path) -> Optional[Dict[str, Any]]:
    """``git fetch origin`` with prune. Returns an error dict on failure."""
    result, err = _git_or_error(
        repo_path,
        ["fetch", "--prune", "--no-tags", "origin"],
        timeout=_DEFAULT_FETCH_TIMEOUT_S,
        op_label="fetch",
    )
    if err is not None:
        return err
    assert result is not None
    if result.returncode != 0:
        return _err(
            SyncErrorCode.GIT_COMMAND_FAILED,
            "git fetch origin failed",
            details={
                "exit_code": result.returncode,
                "stderr": result.stderr.strip()[:2000],
            },
        )
    log_event(
        log,
        "repo_sync.fetch_ok",
        "git fetch origin completed",
        stderr_len=len(result.stderr),
    )
    return None


def _rev_parse(repo_path: Path, ref: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve ``ref`` to a 40-char SHA via ``git rev-parse --verify``."""
    result, err = _git_or_error(
        repo_path,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        timeout=_DEFAULT_FAST_TIMEOUT_S,
        op_label=f"rev-parse {ref}",
    )
    if err is not None:
        return None, err
    assert result is not None
    if result.returncode != 0:
        return None, _err(
            SyncErrorCode.GIT_COMMAND_FAILED,
            f"git rev-parse failed for {ref!r}",
            details={
                "exit_code": result.returncode,
                "stderr": result.stderr.strip()[:500],
            },
        )
    sha = result.stdout.strip()
    if not sha:
        return None, _err(
            SyncErrorCode.GIT_COMMAND_FAILED,
            f"git rev-parse returned empty SHA for {ref!r}",
        )
    return sha, None


def _detect_changes(
    repo_path: Path,
    old_sha: str,
    new_sha: str,
) -> Tuple[List[str], List[str], List[Dict[str, str]], Optional[Dict[str, Any]]]:
    """Compute changed / deleted / renamed files between two SHAs.

    Uses the porcelain plumbing form (``--name-status -z`` with rename and
    copy detection on); no human-readable parsing is performed.
    """
    result, err = _git_or_error(
        repo_path,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            f"{old_sha}..{new_sha}",
        ],
        timeout=_DEFAULT_MERGE_TIMEOUT_S,
        op_label="diff name-status",
    )
    if err is not None:
        return [], [], [], err
    assert result is not None
    if result.returncode != 0:
        return [], [], [], _err(
            SyncErrorCode.GIT_COMMAND_FAILED,
            "git diff --name-status failed",
            details={
                "exit_code": result.returncode,
                "stderr": result.stderr.strip()[:500],
            },
        )
    changed, deleted, renamed = _parse_name_status_z(result.stdout)
    return changed, deleted, renamed, None


def _ensure_clean_work_tree(repo_path: Path) -> Optional[Dict[str, Any]]:
    """Refuse to proceed if there are tracked-file changes locally.

    Uses ``git status --porcelain=v1 -z`` and treats *any* non-empty output
    as dirty. We do not honour ``--ignored`` paths (untracked + ignored
    files are fine — fast-forward only mutates tracked content).
    """
    result, err = _git_or_error(
        repo_path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=no"],
        timeout=_DEFAULT_FAST_TIMEOUT_S,
        op_label="status",
    )
    if err is not None:
        return err
    assert result is not None
    if result.returncode != 0:
        return _err(
            SyncErrorCode.GIT_COMMAND_FAILED,
            "git status --porcelain failed",
            details={
                "exit_code": result.returncode,
                "stderr": result.stderr.strip()[:500],
            },
        )
    if result.stdout:
        sample = [tok for tok in result.stdout.split("\x00") if tok][:10]
        return _err(
            SyncErrorCode.DIRTY_WORK_TREE,
            "refusing to fast-forward: local working tree has uncommitted changes",
            details={"entries_sample": sample},
        )
    return None


def _fast_forward_merge(repo_path: Path, tracked_branch: str) -> Optional[Dict[str, Any]]:
    """``git merge --ff-only origin/<tracked_branch>``.

    Avoids force/reset semantics by design. Detached HEAD or a divergent
    history is surfaced as :data:`SyncErrorCode.NON_FAST_FORWARD`.
    """
    result, err = _git_or_error(
        repo_path,
        ["merge", "--ff-only", f"origin/{tracked_branch}"],
        timeout=_DEFAULT_MERGE_TIMEOUT_S,
        op_label="merge --ff-only",
    )
    if err is not None:
        return err
    assert result is not None
    if result.returncode == 0:
        return None
    stderr = result.stderr.strip()
    lowered = stderr.lower()
    if (
        "non-fast-forward" in lowered
        or "not possible to fast-forward" in lowered
        or "diverg" in lowered
        or "refusing to merge unrelated histories" in lowered
        or "you are not currently on a branch" in lowered
    ):
        return _err(
            SyncErrorCode.NON_FAST_FORWARD,
            "cannot fast-forward: history has diverged or HEAD is detached",
            details={"stderr": stderr[:1000]},
        )
    return _err(
        SyncErrorCode.GIT_COMMAND_FAILED,
        "git merge --ff-only failed",
        details={"exit_code": result.returncode, "stderr": stderr[:1000]},
    )


# --------------------------------------------------------------------------- #
# Module-level guard                                                          #
# --------------------------------------------------------------------------- #


def _git_available() -> bool:
    """Cheap PATH check used by tests; not part of the contract."""
    return shutil.which("git") is not None


__all__ = [
    "RepoSyncError",
    "SyncErrorCode",
    "sync_repository",
]
