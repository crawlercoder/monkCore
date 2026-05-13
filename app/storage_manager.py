"""Filesystem layout for the AI agent (repos, artifacts, cache, tmp).

The root directory is :attr:`app.config.Settings.base_storage_path`
(environment variable ``BASE_STORAGE_PATH``, default ``/ai-agent`` at the **volume root**).
Legacy ``AGENT_SYSTEM_ROOT`` is still read for that value if the new name
is unset — see :mod:`app.config`. All directory creation operations are
idempotent (safe to call repeatedly).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from app.config import get_settings
from app.logging import get_logger

log = get_logger(__name__)

_SUBDIRS: Final[tuple[str, ...]] = ("repos", "artifacts", "cache", "tmp")


def _root_dir() -> Path:
    """Resolve the base root from :func:`get_settings` (``BASE_STORAGE_PATH``)."""
    raw = (get_settings().base_storage_path or "").strip()
    if not raw:
        raw = "/ai-agent"
    return Path(os.path.expanduser(raw)).resolve()


def _trailing_os_sep(path: Path) -> str:
    s = str(path.resolve())
    if not s.endswith(os.sep):
        s += os.sep
    return s


def _path_segment(name: str, value: str) -> str:
    """Return a single safe path component (no traversal)."""
    s = (value or "").strip()
    if not s:
        raise ValueError(f"{name} must be a non-empty string")
    if s in (".", "..") or ".." in s:
        raise ValueError(f"{name} must not contain '..' or be '.' or '..'")
    if any(sep in s for sep in ("/", "\\")):
        raise ValueError(f"{name} must be a single path segment (no slashes)")
    return s


def init_storage() -> None:
    """Create ``<root>/repos``, ``artifacts``, ``cache``, and ``tmp`` if missing.

    Idempotent: existing directories are left unchanged. Parents are
    created as needed.
    """
    base = _root_dir()
    log.info(
        "storage.init: ensuring layout under %s (config base_storage_path=%r)",
        base,
        get_settings().base_storage_path,
    )
    for name in _SUBDIRS:
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        log.info("storage.init: ready %s", d)


def get_repo_path(org_id: str, repo_id: str) -> str:
    """Return ``<root>/repos/{org_id}/{repo_id}/`` as a string (trailing separator)."""
    oid = _path_segment("org_id", org_id)
    rid = _path_segment("repo_id", repo_id)
    p = _root_dir() / "repos" / oid / rid
    return _trailing_os_sep(p)


def get_artifact_path(job_id: str) -> str:
    """Return ``<root>/artifacts/{job_id}/`` as a string (trailing separator)."""
    jid = _path_segment("job_id", job_id)
    p = _root_dir() / "artifacts" / jid
    return _trailing_os_sep(p)


def get_tmp_path(job_id: str) -> str:
    """Return ``<root>/tmp/{job_id}/`` as a string (trailing separator)."""
    jid = _path_segment("job_id", job_id)
    p = _root_dir() / "tmp" / jid
    return _trailing_os_sep(p)


def ensure_dir(path: str) -> None:
    """Create ``path`` and parents if they do not exist. Idempotent."""
    if not path or not str(path).strip():
        raise ValueError("path must be a non-empty string")
    p = Path(os.path.expanduser(str(path).strip()))
    before = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if not before and p.is_dir():
        log.info("storage.ensure_dir: created %s", p)
    else:
        log.debug("storage.ensure_dir: exists %s", p)


__all__ = [
    "ensure_dir",
    "get_artifact_path",
    "get_tmp_path",
    "get_repo_path",
    "init_storage",
]
