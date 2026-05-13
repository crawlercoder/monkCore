"""Walk a repository and collect text files for language-aware indexing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final, List

from app.logging import get_logger

log = get_logger(__name__)

# --- size rules (see module doc in scan_repo) ---
_MAX_FILE_BYTES: Final[int] = 2 * 1024 * 1024
_LARGE_THRESHOLD: Final[int] = 300 * 1024
# Do not read more than this into memory for a single "large" file.
_READ_CAP: Final[int] = 300 * 1024

# Do not walk into; also skip if any rel path component matches (case-sensitive).
_SKIP_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {".git", "node_modules", "dist", "build", "target"},
)

_EXT_TO_LANGUAGE: Final[dict[str, str]] = {
    ".java": "java",
    ".ts": "typescript",
    ".js": "javascript",
    ".py": "python",
    ".php": "php",
}


def _is_excluded_js(name: str) -> bool:
    n = name.lower()
    return n.endswith(".min.js") or n.endswith(".bundle.js")


def _read_text_safely(path: Path, max_bytes: int) -> str:
    """Read at most ``max_bytes`` of UTF-8 text (errors replaced)."""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        if max_bytes < 0:
            return f.read()
        return f.read(max_bytes + 1)[:max_bytes]


def scan_repo(repo_path: str) -> List[dict[str, Any]]:
    """Return one dict per file under code-only extensions, with size and content rules.

    * Extensions: ``.java``, ``.ts``, ``.js``, ``.php``, ``.py`` only.
    * Skips directories: ``node_modules``, ``.git``, ``dist/``, ``build/``, ``target/``
      when any path segment has one of these names.
    * Skips file names: ``*.min.js``, ``*.bundle.js`` (other ``.js`` files are included).
    * **> 2 MiB:** file omitted (logged).
    * **300 KiB – 2 MiB:** included with ``large_file: true``; only the first
      300 KiB of content is read into the ``content`` field (avoids full load).
    * **< 300 KiB:** ``large_file: false``; full file content in ``content``.

    ``file_path`` is relative to the repository root, POSIX-style. Symlinks
    to files and directories are not followed. Safe to run on an untrusted
    tree: paths are resolved under the repo root.
    """
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repo_path must be an existing directory: {root!s}")

    def _onwalk_error(exc: OSError) -> None:
        log.warning("repo_scanner: walk error: %s", exc)

    results: list[dict[str, Any]] = []
    n_skipped_size = 0

    for dirpath, dirnames, filenames in os.walk(
        root,
        topdown=True,
        onerror=_onwalk_error,
        followlinks=False,
    ):
        dpath = Path(dirpath)
        rel_dir = dpath.relative_to(root) if dpath != root else Path()
        if any(p in _SKIP_DIR_NAMES for p in rel_dir.parts):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]

        for name in filenames:
            path = dpath / name
            if path.is_symlink():
                log.debug("repo_scanner: skip symlink %s", path)
                continue
            if not path.is_file():
                continue

            ext = path.suffix.lower()
            if ext not in _EXT_TO_LANGUAGE:
                continue
            if ext == ".js" and _is_excluded_js(name):
                log.debug("repo_scanner: skip minified/bundle name %s", path)
                continue

            try:
                st = path.stat()
            except OSError as exc:
                log.warning("repo_scanner: stat failed %s: %s", path, exc)
                continue

            size = int(st.st_size)
            if size > _MAX_FILE_BYTES:
                n_skipped_size += 1
                log.debug("repo_scanner: skip (>%sB) %s", _MAX_FILE_BYTES, path)
                continue

            rel = path.relative_to(root).as_posix()
            lang = _EXT_TO_LANGUAGE[ext]
            is_large = size >= _LARGE_THRESHOLD

            if is_large:
                # Partial read: cap bytes read from disk
                read_n = min(size, _READ_CAP)
                try:
                    content = _read_text_safely(path, read_n)
                except OSError as exc:
                    log.warning("repo_scanner: read failed %s: %s", path, exc)
                    continue
            else:
                try:
                    content = _read_text_safely(path, size)
                except OSError as exc:
                    log.warning("repo_scanner: read failed %s: %s", path, exc)
                    continue

            results.append(
                {
                    "file_path": rel,
                    "language": lang,
                    "content": content,
                    "size": size,
                    "large_file": is_large,
                }
            )

    log.info(
        "repo_scanner: scanned %s -> %d files (skipped over size: %d)",
        root, len(results), n_skipped_size,
    )
    return results


__all__ = ["scan_repo"]
