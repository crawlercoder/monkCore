"""Per-job artifact layout under :func:`app.storage_manager.get_artifact_path`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

from app.logging import get_logger
from app import storage_manager

log = get_logger(__name__)

_LOGS: Final[str] = "logs"
_DIFFS: Final[str] = "diffs"
_PLAN: Final[str] = "plan"
_QUESTIONS: Final[str] = "questions"
_SUMMARY: Final[str] = "summary"

_PLAN_FILE: Final[str] = "plan.json"
_QUESTIONS_FILE: Final[str] = "questions.json"
_SUMMARY_FILE: Final[str] = "summary.json"

_DEFAULT_MAX_LOG_LINES: Final[int] = 2000


def _job_root(job_id: str) -> Path:
    raw = storage_manager.get_artifact_path(job_id)
    return Path(raw.rstrip("/\\")).resolve()


def _safe_filename(name: str) -> str:
    s = (name or "").strip()
    if not s:
        raise ValueError("filename must be a non-empty string")
    if s in (".", "..") or ".." in s:
        raise ValueError("invalid filename")
    if any(sep in s for sep in ("/", "\\")):
        raise ValueError("filename must not contain path separators")
    return s


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def init_job_artifacts(job_id: str) -> Path:
    """Create ``<root>/artifacts/{job_id}/`` with the standard subfolders."""
    storage_manager.init_storage()
    base = _job_root(job_id)
    for name in (_LOGS, _DIFFS, _PLAN, _QUESTIONS, _SUMMARY):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
    log.info("artifact_manager: init job %s at %s", job_id, base)
    return base


def write_log(job_id: str, filename: str, content: str) -> Path:
    """Append ``content`` to ``logs/{filename}`` (UTF-8). Creates parent dirs as needed."""
    fn = _safe_filename(filename)
    log_dir = _job_root(job_id) / _LOGS
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / fn
    line = content if (content == "" or content.endswith("\n")) else content + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    log.debug("artifact_manager: append log %s", path)
    return path


def save_diff(job_id: str, filename: str, diff_content: str) -> Path:
    """Write ``diff_content`` to ``diffs/{filename}`` (overwrite, UTF-8)."""
    fn = _safe_filename(filename)
    diff_dir = _job_root(job_id) / _DIFFS
    diff_dir.mkdir(parents=True, exist_ok=True)
    path = diff_dir / fn
    path.write_text(diff_content, encoding="utf-8", newline="\n")
    log.debug("artifact_manager: save diff %s", path)
    return path


def save_plan(job_id: str, plan_json: Any) -> Path:
    """Write structured plan as JSON to ``plan/plan.json``."""
    plan_dir = _job_root(job_id) / _PLAN
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / _PLAN_FILE
    _write_json(path, plan_json)
    log.info("artifact_manager: save plan %s", path)
    return path


def save_questions(job_id: str, questions_json: Any) -> Path:
    """Write questions as JSON to ``questions/questions.json``."""
    qdir = _job_root(job_id) / _QUESTIONS
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / _QUESTIONS_FILE
    _write_json(path, questions_json)
    log.info("artifact_manager: save questions %s", path)
    return path


def save_summary(job_id: str, summary_json: Any) -> Path:
    """Write the end-of-job summary (``mr_url`` / ``staging_url`` / ...) to
    ``summary/summary.json``. Always overwrites atomically.
    """
    sdir = _job_root(job_id) / _SUMMARY
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / _SUMMARY_FILE
    _write_json(path, summary_json)
    log.info("artifact_manager: save summary %s", path)
    return path


def read_summary(job_id: str) -> dict[str, Any] | None:
    """Return the parsed summary JSON for ``job_id`` or ``None`` if missing.

    Returns ``None`` for both "no file yet" and "unreadable/malformed" to
    keep the HTTP layer simple — the caller can treat absence as
    "pipeline hasn't produced results yet".
    """
    path = _job_root(job_id) / _SUMMARY / _SUMMARY_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("artifact_manager: unreadable summary %s", path)
        return None
    return data if isinstance(data, dict) else None


def read_logs(
    job_id: str,
    *,
    max_lines: int = _DEFAULT_MAX_LOG_LINES,
) -> list[str]:
    """Concatenate lines from every file under ``logs/`` for ``job_id``.

    Files are visited in stable sorted order so output is deterministic
    even when multiple log files coexist (e.g. ``pipeline.log``,
    ``worker.log``). Trailing newlines are stripped. Empty list if the
    log directory doesn't exist yet.
    """
    log_dir = _job_root(job_id) / _LOGS
    if not log_dir.is_dir():
        return []
    try:
        files = sorted(p for p in log_dir.iterdir() if p.is_file())
    except OSError:
        return []
    lines: list[str] = []
    for p in files:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    lines.append(ln.rstrip("\n"))
        except OSError:
            continue
    if max_lines > 0 and len(lines) > max_lines:
        # Keep the tail — the most recent activity is almost always
        # what callers want to see.
        return lines[-max_lines:]
    return lines


__all__ = [
    "init_job_artifacts",
    "read_logs",
    "read_summary",
    "save_diff",
    "save_plan",
    "save_questions",
    "save_summary",
    "write_log",
]
