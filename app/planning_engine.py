"""LLM-based implementation plan from a **spec** (primary) and **context** (support).

:func:`generate_plan` takes the product/change string as the first argument and
RAG/retrieval output as the second. The spec alone defines *what* to build;
``context`` (e.g. from :func:`app.context_builder.build_context`) only *grounds*
the plan in the codebase—if anything appears to conflict, follow **spec**.

Uses the same Bedrock / Anthropic (and Titan / Llama) patterns as
:mod:`app.code_understanding`. Output is a single JSON object; embeddings live
in the caller, not here.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Final, TypedDict

from botocore.exceptions import ClientError

from app.bedrock_runtime import get_bedrock_runtime
from app.code_understanding import (
    _is_anthropic,
    _is_meta_llama,
    _is_titan_text,
    _first_json_object,
    _strip_code_fences,
)
from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger

log = get_logger(__name__)

_DEFAULT_TEMPERATURE: Final[float] = 0.2
_DEFAULT_TOP_P: Final[float] = 1.0
_APP_RETRY_MULTIPLIER: Final[int] = 3

_RETRIABLE: Final[frozenset[str]] = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "TooManyRequestsException",
        "InternalServerException",
        "ModelTimeoutException",
    }
)

_ALLOWED_REPO_TYPE: Final[frozenset[str]] = frozenset(
    {"frontend", "backend", "config", "test"}
)

_PARSE_RETRIES: Final[int] = 2

_STRICT_FOLLOW: Final[str] = (
    "\n\nReturn ONLY one JSON object matching the schema. No markdown, no code fences, no text outside the object."
)

_SYSTEM: Final[str] = (
    "You are a staff engineer producing an **implementation plan** as JSON only.\n"
    "You will see two parts:\n"
    "1) **Spec** — the authoritative product/change request; this is the primary"
    " input. Your plan must satisfy the spec.\n"
    "2) **Supporting context** — JSON with retrieved code chunks and related data;"
    " use it to name repos, files, and layers, but it is *supporting* only. If"
    " context is thin or seems at odds with the spec, **the spec wins**.\n"
    "Do not copy large code into the plan; reference paths when useful.\n\n"
    "Your job:\n"
    "- Split impact across **frontend** and **backend** (and other repo kinds) using\n"
    "  the *context*; infer from paths (e.g. package.json, .tsx, Next → frontend;\n"
    "  app/api, FastAPI, services → backend).\n"
    "- If the work touches HTTP, REST, or RPC, call out **API changes** explicitly\n"
    "  in task descriptions and list likely server/client files.\n"
    "- Include **testing**: add or update unit/integration tests (name typical test\n"
    "  paths, e.g. tests/, __tests__/, spec/).\n"
    "- If a **feature flag** is appropriate (rollout risk, A/B, kill-switch), set\n"
    "  `feature_flag.required` true and propose a `flag_name` (snake_case or similar);\n"
    "  if not needed, set `required` to false and `flag_name` to an empty string.\n"
    "- Be **specific about file paths** when the context or spec allows; use\n"
    "  repository-relative paths and leave `files_to_modify` empty when unknown.\n\n"
    "Schema (all keys required):\n"
    "{\n"
    '  "repos": [\n'
    '    { "repo_id": string, "type": "frontend" | "backend" | "config" | "test" }\n'
    "  ],\n"
    '  "tasks": [\n'
    "    {\n"
    '      "repo_id": string,\n'
    '      "description": string,\n'
    '      "files_to_modify": [ string, ... ]\n'
    "    }\n"
    "  ],\n"
    '  "feature_flag": { "required": boolean, "flag_name": string }\n'
    "}\n"
    "Rules: Output **only** that JSON. No other keys. No comments inside JSON."
)


def _max_gen_tokens() -> int:
    s = get_settings()
    return max(256, min(int(s.bedrock.max_tokens), 8_000))


def _invoke_anthropic_flex(
    client: Any, model_id: str, system: str, user: str, max_tokens: int
) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": _DEFAULT_TEMPERATURE,
        "top_p": _DEFAULT_TOP_P,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    payload = json.loads(resp["body"].read())
    parts: list[str] = []
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "".join(parts).strip()


def _invoke_titan_flex(
    client: Any, model_id: str, system: str, user: str, max_tokens: int
) -> str:
    body = {
        "inputText": f"{system}\n\n{user}",
        "textGenerationConfig": {
            "maxTokenCount": max_tokens,
            "temperature": _DEFAULT_TEMPERATURE,
            "topP": _DEFAULT_TOP_P,
        },
    }
    resp = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    payload = json.loads(resp["body"].read())
    results = payload.get("results") or []
    if not results:
        return ""
    return str(results[0].get("outputText", "")).strip()


def _invoke_llama_flex(
    client: Any, model_id: str, system: str, user: str, max_tokens: int
) -> str:
    body = {
        "prompt": f"<|system|>\n{system}\n<|user|>\n{user}\n<|assistant|>\n",
        "max_gen_len": max_tokens,
        "temperature": _DEFAULT_TEMPERATURE,
        "top_p": _DEFAULT_TOP_P,
    }
    resp = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    payload = json.loads(resp["body"].read())
    return str(payload.get("generation", "")).strip()


def _dispatch_flex(
    client: Any, model_id: str, system: str, user: str, max_tokens: int
) -> str:
    if _is_anthropic(model_id):
        return _invoke_anthropic_flex(client, model_id, system, user, max_tokens)
    if _is_meta_llama(model_id):
        return _invoke_llama_flex(client, model_id, system, user, max_tokens)
    if _is_titan_text(model_id):
        return _invoke_titan_flex(client, model_id, system, user, max_tokens)
    log.info(
        "planning_engine: unknown model family; using Anthropic payload model_id=%s",
        model_id,
    )
    return _invoke_anthropic_flex(client, model_id, system, user, max_tokens)


def _invoke_planning_llm(
    system: str,
    user: str,
    *,
    model_id: str,
    max_output_tokens: int | None = None,
) -> str:
    client = get_bedrock_runtime()
    s = get_settings()
    max_attempts = max(1, s.bedrock.max_retries) * _APP_RETRY_MULTIPLIER
    if max_output_tokens is not None:
        mt = max(256, min(int(max_output_tokens), 16_000))
    else:
        mt = _max_gen_tokens()
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        t0 = time.perf_counter()
        try:
            text = _dispatch_flex(client, model_id, system, user, mt)
            log.info(
                "planning_engine: LLM ok attempt=%s model_id=%s ms=%.1f chars=%s",
                attempt,
                model_id,
                (time.perf_counter() - t0) * 1000.0,
                len(text),
            )
            return text
        except (ClientError, OSError, ValueError, json.JSONDecodeError) as e:
            last = e
            code = ""
            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
            if attempt < max_attempts and code in _RETRIABLE:
                time.sleep(
                    min(2.0 ** (attempt - 1) + random.random() * 0.25, 30.0)
                )
                continue
            log.error("planning_engine: LLM failed model_id=%s: %s", model_id, e)
            raise
    if last:
        raise last
    raise RuntimeError("planning_engine: unreachable retry state")


def _parse_plan_json(text: str) -> dict[str, Any]:
    raw = _first_json_object(_strip_code_fences(text))
    if not raw:
        raise ValueError("no JSON object in LLM output")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("LLM output root must be an object")
    return obj


def _norm_repo_type(t: str) -> str:
    s = t.strip().lower()
    if s in _ALLOWED_REPO_TYPE:
        return s
    if "front" in s or "ui" in s or "client" in s or "web" in s or "next" in s:
        return "frontend"
    if "back" in s or "api" in s or "server" in s or "service" in s:
        return "backend"
    if "test" in s or "e2e" in s or "spec" in s:
        return "test"
    if "config" in s or "infra" in s or "env" in s:
        return "config"
    return "backend"


def _coerce_plan(data: dict[str, Any]) -> dict[str, Any]:
    repos_out: list[dict[str, str]] = []
    for r in data.get("repos") or []:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("repo_id", "")).strip()
        if not rid:
            continue
        typ = _norm_repo_type(str(r.get("type", "")))
        repos_out.append({"repo_id": rid, "type": typ})

    tasks_out: list[dict[str, Any]] = []
    for t in data.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        rid = str(t.get("repo_id", "")).strip()
        desc = str(t.get("description", "")).strip()
        if not desc:
            desc = "Implementation task"
        files_raw = t.get("files_to_modify")
        files: list[str] = []
        if isinstance(files_raw, str) and files_raw.strip():
            files = [files_raw.strip()]
        elif isinstance(files_raw, (list, tuple)):
            for p in files_raw:
                s = p.strip() if isinstance(p, str) else str(p).strip()
                if s:
                    files.append(s)
        entry: dict[str, Any] = {
            "repo_id": rid,
            "description": desc,
            "files_to_modify": files,
        }
        tasks_out.append(entry)

    ff = data.get("feature_flag")
    if not isinstance(ff, dict):
        ff = {}
    req = ff.get("required")
    if not isinstance(req, bool):
        req = bool(req)
    fn = str(ff.get("flag_name", "")).strip()
    if not req:
        fn = fn or ""
    return {
        "repos": repos_out,
        "tasks": tasks_out,
        "feature_flag": {"required": req, "flag_name": fn},
    }


def _supporting_context_only(context: dict) -> dict:
    """Strip duplicate ``spec`` from RAG bundle so the param ``spec`` is the only copy.

    :func:`app.context_builder.build_context` returns ``{"spec", "context": [...]}``;
    the first argument to :func:`generate_plan` is that same spec — we do not
    echo it again inside the JSON block.
    """
    if not context:
        return {}
    return {k: v for k, v in context.items() if k != "spec"}


def _user_payload(spec: str, context: dict) -> str:
    support = _supporting_context_only(context) if isinstance(context, dict) else {}
    try:
        ctx_json = json.dumps(support, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        ctx_json = json.dumps(
            str(support)[:50_000], ensure_ascii=False
        )
    return (
        f"## Spec (primary input — follow this)\n{spec}\n\n"
        f"## Supporting context (RAG / codebase grounding — not a second spec)\n{ctx_json}"
    )


# --- public types (schema matches the prompt) --------------------------------


class _RepoItem(TypedDict):
    repo_id: str
    type: str


class _TaskItem(TypedDict):
    repo_id: str
    description: str
    files_to_modify: list[str]


class _FeatureFlagBlock(TypedDict):
    required: bool
    flag_name: str


class _Plan(TypedDict):
    repos: list[_RepoItem]
    tasks: list[_TaskItem]
    feature_flag: _FeatureFlagBlock


def generate_plan(spec: str, context: dict) -> dict[str, Any]:
    """Produce a structured implementation plan (Bedrock LLM, JSON only after coaxing).

    **spec** is the primary input: the product or change request (typically the
    job’s stored spec string). **context** supports planning—usually the result
    of :func:`app.context_builder.build_context` (retrieved chunks). The model
    is instructed to satisfy **spec** and use **context** only to ground the plan
    in the codebase; a duplicate ``context["spec"]`` is not sent to the LLM
    (see :func:`_supporting_context_only`).

    Parameters
    ----------
    spec
        Authoritative what-to-build / what-to-change text.
    context
        JSON-serializable dict, typically ``{ "context": [ { repo_id, file, code } ] }``;
        a ``"spec"`` key is stripped when building the user message to avoid
        diluting the primary **spec** argument.

    Returns
    -------
    dict
        ``{ "repos": [...], "tasks": [...], "feature_flag": { "required", "flag_name" } }``
    """
    s = (spec or "").strip()
    if not s:
        raise ValueError("spec must be non-empty")
    if not isinstance(context, dict):
        raise TypeError("context must be a dict")
    s_obj = get_settings()
    model_id = resolve_bedrock_text_model_id_for_region(
        s_obj.bedrock.model_id, s_obj.bedrock_region
    )
    user = _user_payload(s, context)
    if len(user) > 100_000:
        user = user[:100_000] + "\n\n[… context truncated …]"

    last_err: Exception | None = None
    for att in range(1, _PARSE_RETRIES + 1):
        u = user
        if att > 1:
            u = user + _STRICT_FOLLOW
        try:
            text = _invoke_planning_llm(_SYSTEM, u, model_id=model_id)
            data = _parse_plan_json(text)
            return _coerce_plan(data)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            log.warning("planning_engine: parse failed attempt=%s: %s", att, e)
    if last_err:
        raise ValueError("planning: could not obtain valid plan JSON") from last_err
    raise RuntimeError("planning_engine: unreachable")


__all__ = ["generate_plan"]
