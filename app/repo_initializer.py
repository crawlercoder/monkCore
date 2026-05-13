"""Post-clone repository layout: ``.agent/`` metadata for the AI agent."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from app.logging import get_logger

log = get_logger(__name__)

_AGENT_DIRNAME = ".agent"
_METADATA_NAME = "metadata.json"
_BRANCH_INFO_NAME = "branch_info.json"


def _repo_root(repo_path: str | Path) -> Path:
    return Path(repo_path).expanduser().resolve()


def _agent_dir(repo_path: str | Path) -> Path:
    return _repo_root(repo_path) / _AGENT_DIRNAME


def _metadata_path(repo_path: str | Path) -> Path:
    return _agent_dir(repo_path) / _METADATA_NAME


def _branch_info_path(repo_path: str | Path) -> Path:
    return _agent_dir(repo_path) / _BRANCH_INFO_NAME


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically (temp in same directory, then ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(data, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("repo_initializer: read failed %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("repo_initializer: invalid JSON in %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("repo_initializer: expected object in %s, got %s", path, type(data))
        return None
    return data


def init_repo_metadata(
    repo_path: str | Path,
    org_id: str,
    repo_id: str,
) -> None:
    """Create ``.agent/`` with ``metadata.json`` and ``branch_info.json`` if missing.

    Does **not** overwrite existing files: present files are left unchanged
    and a log line is emitted at INFO.
    """
    if not (org_id or "").strip() or not (repo_id or "").strip():
        raise ValueError("org_id and repo_id must be non-empty strings")

    base = _repo_root(repo_path)
    agent = _agent_dir(repo_path)
    agent.mkdir(parents=True, exist_ok=True)
    log.info("repo_initializer: agent dir %s", agent)

    meta_path = _metadata_path(repo_path)
    if meta_path.exists():
        log.info("repo_initializer: skip existing %s", meta_path)
    else:
        meta = {
            "repo_id": repo_id.strip(),
            "org_id": org_id.strip(),
            "default_branch": "main",
            "status": "CLONED",
            "last_indexed": None,
        }
        _atomic_write_json(meta_path, meta)
        log.info("repo_initializer: wrote %s", meta_path)

    branch_path = _branch_info_path(repo_path)
    if branch_path.exists():
        log.info("repo_initializer: skip existing %s", branch_path)
    else:
        branch_info = {
            "active_branch": "main",
            "agent_branches": [],
        }
        _atomic_write_json(branch_path, branch_info)
        log.info("repo_initializer: wrote %s", branch_path)

    if not base.is_dir():
        log.warning("repo_initializer: repo_path is not a directory: %s", base)


def update_repo_status(repo_path: str | Path, status: str) -> bool:
    """Set ``status`` in ``metadata.json``. Returns True if written.

    If ``metadata.json`` is missing, logs a warning and returns False without
    creating a new file. If JSON is unreadable, returns False.
    """
    if not (status or "").strip():
        raise ValueError("status must be a non-empty string")

    path = _metadata_path(repo_path)
    current = _read_json_file(path)
    if current is None:
        if path.exists():
            log.warning("repo_initializer: cannot update status, bad metadata: %s", path)
        else:
            log.warning("repo_initializer: no metadata to update: %s", path)
        return False

    current["status"] = status.strip()
    _atomic_write_json(path, current)
    log.info("repo_initializer: status -> %r (%s)", status.strip(), path)
    return True


def merge_metadata(repo_path: str | Path, fields: dict[str, Any]) -> bool:
    """Merge ``fields`` into ``.agent/metadata.json``. Returns True if written.

    Does nothing and returns False if the file is missing or not a JSON object.
    """
    if not fields:
        raise ValueError("fields must be non-empty")
    path = _metadata_path(repo_path)
    current = _read_json_file(path)
    if current is None:
        log.warning("repo_initializer: cannot merge, missing or bad %s", path)
        return False
    for k, v in fields.items():
        current[k] = v
    _atomic_write_json(path, current)
    log.info("repo_initializer: metadata merged in %s", path)
    return True


def read_metadata(repo_path: str | Path) -> Optional[dict[str, Any]]:
    """Load ``.agent/metadata.json``. Returns ``None`` if missing or invalid."""
    return _read_json_file(_metadata_path(repo_path))


__all__ = [
    "init_repo_metadata",
    "merge_metadata",
    "read_metadata",
    "update_repo_status",
]
