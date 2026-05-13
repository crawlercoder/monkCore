"""LLM-based concrete file changes for a single repo from a plan + RAG context.

The **Feature spec** (job string) and **context code** (retrieved chunks) are both
in the user prompt. The spec drives *what* to build and *what* to modify; plan
tasks and context snippets support that. See :func:`_build_user_message`.
"""

from __future__ import annotations

import json
from typing import Any, Final

from app.code_understanding import _first_json_object, _strip_code_fences
from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger
from app.planning_engine import _invoke_planning_llm, _STRICT_FOLLOW, _parse_plan_json

log = get_logger(__name__)

_PARSE_RETRIES: Final[int] = 2
_MAX_USER_JSON_CHARS: Final[int] = 95_000
_MAX_CODE_PER_CONTEXT_ITEM: Final[int] = 5_000


def _code_gen_max_tokens() -> int:
    s = get_settings()
    return max(4096, min(int(s.bedrock.max_tokens) * 2, 16_000))


_SYSTEM: Final[str] = (
    "You are a senior engineer. You output **one JSON object only** (no markdown,"
    " no code fences, no explanation).\n"
    "The user message has two human-readable parts you must use together:\n"
    "1) **Feature spec** — the product/change request. It is the **primary**"
    " driver for *what* to build, *which* behaviours to add or fix, and *which*"
    " areas of the repo to touch when choosing what to modify or create.\n"
    "2) **Context code and plan inputs** — JSON with ``context_chunks`` (retrieved"
    " file snippets for this repo), ``plan_tasks`` (scoped tasks from the plan),"
    " and related plan metadata. This is **supporting** material: it shows"
    " existing code to edit and suggested paths, but the **Feature spec** wins"
    " if a task or chunk seems misaligned with the spec.\n\n"
    "Produce **concrete** ``changes`` for this repository only.\n"
    "Hard requirements:\n"
    "- **Satisfy the Feature spec** first; use plan tasks and context code to"
    "  choose files and implement behaviour.\n"
    "- **Minimal changes** — do the smallest change that satisfies the spec"
    "  (as refined by the tasks).\n"
    "- **Respect existing code** — keep structure, style, and naming; do not"
    "  rename, reformat, or ‘clean up’ unrelated code.\n"
    "- **No unnecessary rewrites** — if a one-line or targeted edit is enough,"
    "  do that; for ``modify`` return the **full new file content** (not a"
    "  fragment) only when a whole-file change is the minimal reasonable fix;"
    "  for small edits you may return the full file to keep patches applyable"
    "  downstream, but never duplicate the entire codebase.\n"
    "- Use ``change_type`` ``create`` for **new** files; ``modify`` for"
    "  existing files that appear in the context or are listed in the tasks.\n"
    "- If you cannot know the content, emit no entry for that file rather than"
    "  placeholder filler.\n"
    "Schema (exactly):\n"
    "{\n"
    '  "changes": [\n'
    "    {\n"
    '      "file": string,           // repository-relative path\n'
    '      "change_type": "modify" | "create",\n'
    '      "code": string            // file body after the change, UTF-8\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "The ``code`` string must be the full file for both create and modify (so"
    " callers can write the file directly)."
)


def _norm_rid(repo_id: str) -> str:
    return (repo_id or "").strip()


def _filter_tasks_for_repo(plan: dict[str, Any], repo_id: str) -> list[dict[str, Any]]:
    want = _norm_rid(repo_id)
    out: list[dict[str, Any]] = []
    for t in plan.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        if _norm_rid(str(t.get("repo_id", ""))) == want:
            out.append(
                {
                    "repo_id": str(t.get("repo_id", "")).strip(),
                    "description": str(t.get("description", "")).strip(),
                    "files_to_modify": t.get("files_to_modify"),
                }
            )
    return out


def _context_paths_from_tasks(tasks: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for t in tasks:
        raw = t.get("files_to_modify")
        if isinstance(raw, str) and raw.strip():
            paths.add(raw.strip().replace("\\", "/"))
        elif isinstance(raw, (list, tuple)):
            for p in raw:
                s = p.strip() if isinstance(p, str) else str(p).strip()
                if s:
                    paths.add(s.replace("\\", "/"))
    return paths


def _filter_and_prioritize_context(
    context: dict[str, Any], repo_id: str, task_files: set[str]
) -> list[dict[str, str]]:
    """Keep only chunks for ``repo_id``; list files in ``task_files`` first, trim code when large."""
    want = _norm_rid(repo_id)
    items: list[dict[str, Any]] = []
    for item in (context or {}).get("context") or []:
        if not isinstance(item, dict):
            continue
        if _norm_rid(str(item.get("repo_id", ""))) != want:
            continue
        f = str(item.get("file", "")).strip().replace("\\", "/")
        code = str(item.get("code", ""))
        if len(code) > _MAX_CODE_PER_CONTEXT_ITEM:
            code = code[: _MAX_CODE_PER_CONTEXT_ITEM] + "\n/* … truncated for prompt … */"
        pri = 0 if f in task_files else 1
        items.append(
            {
                "file": f,
                "code": code,
                "priority": pri,
            }
        )
    items.sort(key=lambda x: (x.get("priority", 1), x.get("file", "")))
    return [{"file": str(i["file"]), "code": str(i["code"])} for i in items]


def _build_user_message(
    spec: str,
    plan: dict[str, Any],
    tasks: list[dict[str, Any]],
    ctx_items: list[dict[str, str]],
    repo_id: str,
) -> str:
    """Assemble the user prompt: a **Feature spec** block, then JSON with plan + **context code**.

    The spec appears once as the labelled feature request (not duplicated only
    inside JSON) so the model sees it as the driver for what to build/modify;
    ``context_chunks`` carry retrieved code for this repo.
    """
    spec_text = (spec or "").strip()
    payload: dict[str, Any] = {
        "repo_id": _norm_rid(repo_id),
        "plan_repos": [
            r
            for r in (plan.get("repos") or [])
            if isinstance(r, dict)
            and _norm_rid(str(r.get("repo_id", ""))) == _norm_rid(repo_id)
        ],
        "plan_tasks": tasks,
        "context_chunks": ctx_items,
        "plan_feature_flag": plan.get("feature_flag"),
    }
    try:
        json_part = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        raise ValueError("cannot serialize user payload to JSON") from e
    if len(json_part) > _MAX_USER_JSON_CHARS:
        json_part = json_part[:_MAX_USER_JSON_CHARS] + "\n\n[… truncated …]\n"

    return (
        "## Feature spec\n\n"
        f"{spec_text}\n\n"
        "Use the spec above to decide **what to build** and **what to change** in "
        "this repository. The JSON block below has **Context code** (per-file "
        "snippets retrieved for this repo) plus plan tasks—apply edits to match "
        "the spec, using the chunks as the ground truth for existing code.\n\n"
        "## Context code and plan inputs (JSON)\n\n"
        f"{json_part}"
    )


def _coerce_changes(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("changes")
    if not isinstance(raw, list):
        return {"changes": []}
    out: list[dict[str, str]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        f = str(c.get("file", "")).strip().replace("\\", "/")
        if not f:
            continue
        ct = str(c.get("change_type", "")).strip().lower()
        if ct not in ("modify", "create"):
            ct = "modify"
        code = str(c.get("code", ""))
        if not code and ct == "create":
            log.warning("code_generation: skipping create with empty code file=%s", f)
            continue
        if not code and ct == "modify":
            log.warning("code_generation: empty code for modify file=%s", f)
        out.append({"file": f, "change_type": ct, "code": code})
    return {"changes": out}


def generate_repo_changes(
    spec: str,
    plan: dict[str, Any],
    context: dict[str, Any],
    repo_id: str,
) -> dict[str, Any]:
    """Propose per-file code changes for ``repo_id`` from the job ``spec``, plan, and RAG context.

    The LLM user message includes a **Feature spec** section (this ``spec``) and
    a **Context code** section (JSON with ``context_chunks`` + plan tasks). The
    spec drives what to build and what to modify; chunks provide code to edit.

    ``spec`` must be the same string stored on the job row (not inferred from
    ``context``); :func:`~app.mr_pipeline.run_job` passes it through from
    DynamoDB.

    Steps:

    1. Keep only ``plan`` tasks whose ``repo_id`` matches.
    2. Keep only ``context`` chunks whose ``repo_id`` matches, prioritizing
       files mentioned in those tasks.
    3. Call the Bedrock text model; parse JSON
       ``{ "changes": [ { "file", "change_type", "code" } ] }``.

    If there are no tasks and no context for the repo, returns
    ``{"changes": []}`` without an LLM call.
    """
    if not isinstance(plan, dict):
        raise TypeError("plan must be a dict")
    if not isinstance(context, dict):
        raise TypeError("context must be a dict")
    rid = _norm_rid(repo_id)
    if not rid:
        raise ValueError("repo_id must be non-empty")

    tasks = _filter_tasks_for_repo(plan, rid)
    task_files = _context_paths_from_tasks(tasks)
    ctx_items = _filter_and_prioritize_context(context, rid, task_files)
    spec = (spec or "").strip()

    if not tasks and not ctx_items:
        log.info("code_generation: no tasks or context for repo_id=%s; skipping LLM", rid)
        return {"changes": []}

    s_obj = get_settings()
    model_id = resolve_bedrock_text_model_id_for_region(
        s_obj.bedrock.model_id, s_obj.bedrock_region
    )
    user = _build_user_message(spec, plan, tasks, ctx_items, rid)
    last_err: Exception | None = None
    for att in range(1, _PARSE_RETRIES + 1):
        u = user
        if att > 1:
            u = user + _STRICT_FOLLOW
        try:
            text = _invoke_planning_llm(
                _SYSTEM,
                u,
                model_id=model_id,
                max_output_tokens=_code_gen_max_tokens(),
            )
            data = _parse_plan_json(text)
            return _coerce_changes(data)
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("code_generation: parse failed attempt=%s: %s", att, e)
    if last_err:
        raise ValueError("code_generation: could not parse LLM output") from last_err
    raise RuntimeError("code_generation: unreachable")


__all__ = ["generate_repo_changes"]
