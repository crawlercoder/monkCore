"""High-level git clone: resolve storage, clone or update, init ``.agent`` metadata.

This composes :mod:`app.storage_manager`, :mod:`app.services.git_clone`, and
:mod:`app.repo_initializer`. Token-injected HTTPS and retries live in
:class:`GitCloneService` (``oauth2`` user + URL-encoded token, not a raw
``<token>@host`` string — see that module for details).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from app import repo_initializer, storage_manager
from app.logging import get_logger, log_context, log_event
from app.services.git_clone import GitCloneError, GitCloneService

log = get_logger(__name__)

# At least 2 *retries* (first attempt + 2 retries = 3 tries) on transient errors.
_GIT_SVC: GitCloneService = GitCloneService(
    max_retries=2,
    base_backoff_seconds=1.5,
)


def _dest_path_for(org_id: str, repo_id: str) -> Path:
    raw = storage_manager.get_repo_path(org_id, repo_id)
    return Path(raw.rstrip("/\\")).resolve()


def _cleanup_incomplete_checkout(dest: Path) -> None:
    """Best-effort removal of a failed or partial working tree (see :mod:`git_clone`)."""
    # Fresh clone uses ``<name>.tmp``; remove any leftover.
    tmp = dest.parent / f"{dest.name}.tmp"
    if tmp.exists():
        log.warning("git_service: removing failed clone temp %s", tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    if not dest.exists():
        return
    if (dest / ".git").is_dir():
        return
    log.warning("git_service: removing invalid checkout (no .git) %s", dest)
    shutil.rmtree(dest, ignore_errors=True)


async def clone_repo(
    org_id: str,
    repo_id: str,
    repo_url: str,
    token: str,
    branch: str = "main",
    *,
    clone_service: Optional[GitCloneService] = None,
) -> str:
    """Clone or update a repo at ``<BASE_STORAGE_PATH>/repos/...`` (default ``/ai-agent/repos/...``) and write ``.agent`` files.

    Resolves the destination with :func:`app.storage_manager.get_repo_path`,
    ensures top-level storage with :func:`app.storage_manager.init_storage`,
    runs the git layer (injected token, branch checkout, existing-repo pull —
    all inside :class:`GitCloneService`), then
    :func:`app.repo_initializer.init_repo_metadata`.

    Returns the resolved repo path (same string shape as ``get_repo_path``:
    with trailing path separator). Raises :class:`GitCloneError` on failure;
    on git failure, sibling ``.tmp`` and invalid partial dirs are removed.
    """
    if not (org_id or "").strip() or not (repo_id or "").strip():
        raise ValueError("org_id and repo_id must be non-empty")
    if not (repo_url or "").strip() or not (token or "").strip():
        raise ValueError("repo_url and token must be non-empty")

    branch = (branch or "main").strip()
    storage_manager.init_storage()
    dest = _dest_path_for(org_id, repo_id)
    repo_path_str = storage_manager.get_repo_path(org_id, repo_id)

    svc = clone_service or _GIT_SVC

    with log_context(org_id=org_id.strip(), repo_id=repo_id.strip(), dest=str(dest)):
        log_event(
            log, "git_service.clone_start",
            "storage-backed clone / update",
            branch=branch,
        )
        try:
            path = await svc.clone_repo(
                repo_url.strip(), token, dest, branch=branch, depth=-1
            )
        except GitCloneError as exc:
            log_event(
                log, "git_service.clone_git_failed",
                str(exc),
                level=logging.ERROR,
                error_type=exc.__class__.__name__,
                exc_info=True,
            )
            _cleanup_incomplete_checkout(dest)
            raise
        else:
            log_event(
                log, "git_service.clone_success",
                "git finished; init .agent",
                final_path=str(path),
            )

    try:
        repo_initializer.init_repo_metadata(path, org_id, repo_id)
    except Exception:
        log_event(
            log, "git_service.metadata_failed",
            "clone succeeded but .agent init failed",
            level=logging.ERROR,
            exc_info=True,
        )
        raise

    log_event(log, "git_service.clone_complete", "repo ready", repo_path=repo_path_str)
    return repo_path_str


__all__ = [
    "clone_repo",
    "GitCloneError",
]
