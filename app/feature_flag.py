"""Apply a simple boolean feature flag to generated file changes (LLM + plan)."""

from __future__ import annotations

import json
import re
from typing import Any, Final

from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger
from app.planning_engine import _invoke_planning_llm, _STRICT_FOLLOW, _parse_plan_json

log = get_logger(__name__)

_PARSE_RETRIES: Final[int] = 2
_MAX_USER_JSON_CHARS: Final[int] = 95_000


def _ff_max_tokens() -> int:
    s = get_settings()
    return max(4096, min(int(s.bedrock.max_tokens) * 2, 16_000))


def _flag_block(plan: dict[str, Any]) -> dict[str, Any] | None:
    ff = plan.get("feature_flag")
    if not isinstance(ff, dict):
        return None
    return ff


def _flag_required(plan: dict[str, Any]) -> bool:
    ff = _flag_block(plan)
    if ff is None:
        return False
    r = ff.get("required")
    return r is True


def _sanitize_flag_name(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]", "_", (raw or "").strip())
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s or "feature_flag"


def _resolved_flag_name(plan: dict[str, Any]) -> str:
    ff = _flag_block(plan) or {}
    name = str(ff.get("flag_name", "")).strip()
    return _sanitize_flag_name(name)


_SYSTEM: Final[str] = (
    "You are a senior engineer. Output **one JSON object only** (no markdown,"
    " no code fences).\n"
    "The user passes a **plan** (with ``feature_flag``) and a **changes** list:"
    " each item has ``file``, ``change_type`` (optional), and ``code`` (full file"
    " body after code generation).\n\n"
    "The plan’s feature flag is **required** and must be applied as follows (MVP):\n"
    "- **Boolean** flag only, **default OFF** (``false`` / disabled) in config.\n"
    "- **Create or update one small config/env file** that defines this flag"
    " (e.g. ``.env.example`` line, ``config.py`` constant, ``config/json``, or"
    " framework-appropriate pattern). Use the exact **flag key** given in the"
    " user JSON (``resolved_flag_name``).\n"
    "- **Wrap only the new or changed behavior** in each relevant **application**"
    " file (not test-only files unless the change is test infrastructure):"
    " guard the new logic so it runs only when the flag is ON; when OFF, keep"
    " prior/safe behavior or no-op as appropriate. Do not rewrite unrelated code.\n"
    "- Keep edits **minimal**; preserve style and imports; add small helpers"
    " only if necessary (e.g. ``is_feature_x_enabled()`` reading the config).\n\n"
    "Return shape:\n"
    "{\n"
    '  "changes": [\n'
    "    {\n"
    '      "file": string,\n'
    '      "change_type": "modify" | "create",\n'
    '      "code": string\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Include **all** files the user must write: updated source files **plus**"
    " the config file. Order is not important."
)


def _build_user_message(plan: dict[str, Any], changes: list[Any], flag_name: str) -> str:
    pl = {
        "resolved_flag_name": flag_name,
        "plan_feature_flag": plan.get("feature_flag"),
        "changes": changes,
    }
    try:
        text = json.dumps(pl, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        raise ValueError("plan/changes must be JSON-serializable") from e
    if len(text) > _MAX_USER_JSON_CHARS:
        text = text[:_MAX_USER_JSON_CHARS] + "\n\n[… truncated …]\n"
    return text


def _coerce_changes(data: dict[str, Any]) -> list[dict[str, str]]:
    raw = data.get("changes")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        f = str(c.get("file", "")).strip().replace("\\", "/")
        if not f:
            continue
        out.append({"file": f, "code": str(c.get("code", ""))})
    return out


def _to_file_code_list(changes: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in changes:
        if not isinstance(c, dict):
            continue
        f = str(c.get("file", "")).strip().replace("\\", "/")
        if not f:
            continue
        out.append({"file": f, "code": str(c.get("code", ""))})
    return out


def add_feature_flag(plan: dict[str, Any], changes: list) -> list[dict[str, str]]:
    """Return an updated ``changes`` list with a boolean flag (default OFF) applied.

    If ``plan["feature_flag"]["required"]`` is not true, returns the same content
    as **normalized** items ``{ "file", "code" }`` (LLM is not called).

    When required, calls Bedrock to:

    * add or update a **small config** file holding the flag (default ``false``);
    * **wrap** new logic in application files while keeping edits minimal.

    Each item is ``{ "file", "code" }`` only.
    """
    if not isinstance(plan, dict):
        raise TypeError("plan must be a dict")
    if not isinstance(changes, list):
        raise TypeError("changes must be a list")

    if not _flag_required(plan):
        log.info("feature_flag: not required; returning changes unchanged")
        return _to_file_code_list(changes)

    flag_name = _resolved_flag_name(plan)
    if not changes:
        log.warning(
            "feature_flag: required but changes empty; LLM may emit config-only"
        )

    s_obj = get_settings()
    model_id = resolve_bedrock_text_model_id_for_region(
        s_obj.bedrock.model_id, s_obj.bedrock_region
    )
    user = _build_user_message(plan, changes, flag_name)
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
                max_output_tokens=_ff_max_tokens(),
            )
            data = _parse_plan_json(text)
            if not isinstance(data, dict):
                raise ValueError("LLM output root must be an object")
            return _coerce_changes(data)
        except (TypeError, ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("feature_flag: parse failed attempt=%s: %s", att, e)
    if last_err:
        raise ValueError("feature_flag: could not parse LLM output") from last_err
    raise RuntimeError("feature_flag: unreachable")


__all__ = ["add_feature_flag"]
