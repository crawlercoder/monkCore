"""Generate a structured repository manifest via Bedrock (same invocation path as planning)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.code_understanding import _first_json_object, _strip_code_fences
from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger
from app.planning_engine import (
    _STRICT_FOLLOW,
    _invoke_planning_llm,
    _max_gen_tokens,
)

log = get_logger(__name__)

_README_MAX_CHARS = 12_000
_TREE_MAX_SEGMENTS = 3
_MAX_MODULES = 10
_MAX_FILES_TOTAL = 50

_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".idea",
    ".pytest_cache",
    "dist",
    "build",
})

_MINIMAL: dict[str, Any] = {
    "description": "unknown",
    "tech_stack": [],
    "modules": [],
}

_SYSTEM = (
    "You describe a software repository from the given README excerpt and "
    "directory listing. Output ONLY one JSON object, no markdown fences, no prose.\n\n"
    "Schema (exact keys):\n"
    "{\n"
    '  "description": string,\n'
    '  "tech_stack": string[],\n'
    '  "modules": [\n'
    "    {\n"
    '      "name": string,\n'
    '      "description": string,\n'
    '      "files": string[]  // repository-relative POSIX paths ONLY from the listing provided\n'
    "    }\n"
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    '- Group logically related paths into modules; at most 10 modules.\n'
    "- List ONLY files whose paths appeared in the directory listing "
    "(or are obvious README-only if no files — then use \"files\": [] for that module). "
    "Never invent filenames.\n"
    "- Prefer at most 50 file paths total across all modules "
    '(prioritize the most important).\n'
    '- Each module in your output must have non-empty "name" and "description" '
    'and valid "files" arrays (possibly empty).\n'
)


def _fail_safe() -> dict[str, Any]:
    return dict(_MINIMAL)


def _read_readme(repo: Path) -> str:
    for name in ("README.md", "Readme.md", "readme.md"):
        p = repo / name
        if p.is_file():
            raw = p.read_text(encoding="utf-8", errors="replace")
            if len(raw) > _README_MAX_CHARS:
                return raw[:_README_MAX_CHARS] + "\n\n[… truncated …]\n"
            return raw
    return ""


def _directory_tree(repo: Path) -> str:
    """Paths under repo with at most _TREE_MAX_SEGMENTS segments; skips .git."""
    repo = repo.resolve()
    lines: list[str] = []

    try:
        for dirpath, dirnames, filenames in os.walk(repo, topdown=True):
            rel_dir = Path(dirpath).relative_to(repo)
            if ".git" in rel_dir.parts:
                dirnames[:] = []
                continue
            depth = len(rel_dir.parts)
            if depth >= _TREE_MAX_SEGMENTS:
                dirnames[:] = []
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIR_NAMES)
            # Subdirectories (immediate) that stay within segment budget
            for d in dirnames:
                child = rel_dir / d if rel_dir.parts else Path(d)
                if len(child.parts) <= _TREE_MAX_SEGMENTS:
                    lines.append(child.as_posix() + "/")
            for fname in sorted(filenames):
                fp = Path(dirpath, fname).relative_to(repo)
                if len(fp.parts) <= _TREE_MAX_SEGMENTS:
                    lines.append(fp.as_posix())
        lines.sort(key=lambda x: x.lower())
    except OSError as e:
        log.warning("repo_manifest: tree walk failed: %s", e)
        return ""

    prev: set[str] = set()
    unique: list[str] = []
    for line in lines:
        if line in prev:
            continue
        prev.add(line)
        unique.append(line)

    return "\n".join(unique)


def _build_user_payload(readme_text: str, tree_text: str) -> str:
    parts = ["## Directory tree (max depth 3, no .git)\n", tree_text or "(empty)"]
    if readme_text:
        parts.insert(0, "## README\n\n" + readme_text + "\n\n")
    return "\n".join(parts)


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    raw = _first_json_object(_strip_code_fences(text))
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _normalize_rel_path(repo: Path, s: str) -> str | None:
    t = (s or "").strip().replace("\\", "/").lstrip("/")
    if not t or ".." in Path(t).parts:
        return None
    candidate = (repo / t).resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return Path(t).as_posix()


def _post_process(raw: dict[str, Any], repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    desc = raw.get("description")
    if not isinstance(desc, str) or not desc.strip():
        desc = "unknown"
    else:
        desc = desc.strip()

    tech_raw = raw.get("tech_stack")
    tech_stack: list[str] = []
    if isinstance(tech_raw, list):
        for x in tech_raw:
            if isinstance(x, str) and x.strip():
                tech_stack.append(x.strip())
    elif isinstance(tech_raw, str) and tech_raw.strip():
        tech_stack.append(tech_raw.strip())

    modules_in = raw.get("modules")
    if not isinstance(modules_in, list):
        return {"description": desc, "tech_stack": tech_stack, "modules": []}

    global_seen: set[str] = set()
    modules_out: list[dict[str, Any]] = []
    total_files = 0

    for entry in modules_in:
        if total_files >= _MAX_FILES_TOTAL or len(modules_out) >= _MAX_MODULES:
            break
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        md = entry.get("description")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(md, str) or not md.strip():
            continue
        name = name.strip()
        md = md.strip()

        files_in = entry.get("files")
        ok_files: list[str] = []
        if isinstance(files_in, list):
            for f in files_in:
                if total_files >= _MAX_FILES_TOTAL:
                    break
                if not isinstance(f, str):
                    continue
                np = _normalize_rel_path(repo, f)
                if np is None or np in global_seen:
                    continue
                global_seen.add(np)
                ok_files.append(np)
                total_files += 1

        if not ok_files:
            continue
        modules_out.append({"name": name, "description": md, "files": ok_files})

    # If LLM produced structure but nothing survived validation, strip to empty modules.
    if not modules_out:
        return {"description": desc, "tech_stack": tech_stack, "modules": []}

    return {"description": desc, "tech_stack": tech_stack, "modules": modules_out}


def generate_repo_manifest(repo_path: str) -> dict:
    """Produce a structured manifest for the repository at ``repo_path``.

    On Bedrock / parse / filesystem errors returns
    ``{"description": "unknown", "tech_stack": [], "modules": []}``.
    """
    try:
        root = Path(repo_path).expanduser().resolve()
    except OSError:
        log.warning("repo_manifest: invalid path %r", repo_path)
        return _fail_safe()

    if not root.is_dir():
        log.warning("repo_manifest: not a directory %s", repo_path)
        return _fail_safe()

    readme = _read_readme(root)
    tree = _directory_tree(root)
    user = _build_user_payload(readme, tree)

    s_obj = get_settings()
    model_id = resolve_bedrock_text_model_id_for_region(
        s_obj.bedrock.model_id, s_obj.bedrock_region
    )
    mt = max(_max_gen_tokens(), 4096)

    parsed: dict[str, Any] | None = None
    try:
        for attempt in range(1, 3):
            u = user + (_STRICT_FOLLOW if attempt > 1 else "")
            text = _invoke_planning_llm(_SYSTEM, u, model_id=model_id, max_output_tokens=mt)
            parsed = _parse_llm_json(text)
            if parsed is not None:
                break
        if parsed is None:
            raise ValueError("manifest parse failed")
        return _post_process(parsed, root)
    except Exception as e:
        log.warning("repo_manifest: LLM/post failed for %s: %s", root, e)
        return _fail_safe()


__all__ = ["generate_repo_manifest"]
