"""LLM-based MVP test files for proposed code changes (single repo)."""

from __future__ import annotations

import json
from typing import Any, Final

from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger
from app.planning_engine import _invoke_planning_llm, _STRICT_FOLLOW, _parse_plan_json

log = get_logger(__name__)

_PARSE_RETRIES: Final[int] = 2
_MAX_USER_JSON_CHARS: Final[int] = 95_000


def _test_gen_max_tokens() -> int:
    s = get_settings()
    return max(4096, min(int(s.bedrock.max_tokens) * 2, 16_000))


_SYSTEM: Final[str] = (
    "You are a senior engineer writing **MVP test code** as JSON only (no markdown,"
    " no code fences, no explanation).\n"
    "You are given a ``repo_id`` and a ``changes`` list: each item has at least"
    " ``file``, ``code``, and ``change_type`` (from a code-generation pass).\n\n"
    "Write **small, direct** tests that cover **new or changed** behavior\n"
    " implied by that diff — **basic unit** tests and, where it clearly helps,"
    " a **light integration** test. Keep everything **simple** (MVP), avoid"
    " over-mocking, and match the project’s **existing language and test runner**\n"
    " when inferable from paths (e.g. Python + pytest, JS/TS + vitest or jest).\n"
    "If a change is pure config, skip tests for it unless a trivial smoke check is natural.\n\n"
    "Output **exactly** one JSON object of this form:\n"
    "{\n"
    '  "tests": [ { "file": string, "code": string }, ... ]\n'
    "}\n"
    "Each ``file`` is a **repository-relative** test path. Each ``code`` is the"
    " **full** file body. If no test is needed, return ``\"tests\": []``."
)


def _build_user_message(repo_id: str, changes: list[Any]) -> str:
    pl = {
        "repo_id": (repo_id or "").strip(),
        "changes": changes,
    }
    try:
        text = json.dumps(pl, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        raise ValueError("changes must be JSON-serializable") from e
    if len(text) > _MAX_USER_JSON_CHARS:
        text = text[:_MAX_USER_JSON_CHARS] + "\n\n[… truncated …]\n"
    return text


def _coerce_tests(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("tests")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        f = str(item.get("file", "")).strip().replace("\\", "/")
        if not f:
            continue
        code = str(item.get("code", ""))
        out.append({"file": f, "code": code})
    return out


def generate_tests(repo_id: str, changes: list) -> list[dict[str, str]]:
    """Return proposed test file(s) for ``changes`` in ``repo_id`` (LLM, Bedrock).

    Parameters
    ----------
    repo_id
        Repository the changes belong to.
    changes
        Typically items like ``{ "file", "change_type", "code" }`` from
        :func:`app.code_generation.generate_repo_changes`. Empty list skips the LLM
        and returns ``[]``.

    Returns
    -------
    list[dict]
        ``[ { "file": str, "code": str }, ... ]`` — one object per test file.
    """
    rid = (repo_id or "").strip()
    if not rid:
        raise ValueError("repo_id must be non-empty")
    if not isinstance(changes, list):
        raise TypeError("changes must be a list")
    if not changes:
        log.info("test_generator: empty changes for repo_id=%s; skipping LLM", rid)
        return []

    s_obj = get_settings()
    model_id = resolve_bedrock_text_model_id_for_region(
        s_obj.bedrock.model_id, s_obj.bedrock_region
    )
    user = _build_user_message(rid, changes)
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
                max_output_tokens=_test_gen_max_tokens(),
            )
            data = _parse_plan_json(text)
            if not isinstance(data, dict):
                raise ValueError("LLM output root must be an object")
            return _coerce_tests(data)
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("test_generator: parse failed attempt=%s: %s", att, e)
    if last_err:
        raise ValueError("test_generator: could not parse LLM output") from last_err
    raise RuntimeError("test_generator: unreachable")


__all__ = ["generate_tests"]
