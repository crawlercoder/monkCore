"""LLM-backed code understanding.

Given a single code chunk (as produced by :mod:`app.code_chunker`), produce a
small, structured JSON document that describes what it is, why it exists, and
what it talks to. The output schema is:

    {
        "summary":      str,
        "purpose":      str,
        "inputs":       list[str],
        "outputs":      list[str],
        "dependencies": list[str],
        "type":         "function" | "class" | "api" | "utility"
    }

The module talks to Amazon Bedrock directly (Anthropic Messages API by default,
with Titan-text and Meta-Llama payload fallbacks). It shares the same
retry / logging conventions as :mod:`app.embedding_service`:

* application-level retry on top of botocore's built-in adaptive retries;
* exponential backoff with jitter, bounded by ``BEDROCK_MAX_RETRIES``;
* one extra round-trip if the first LLM response is not valid JSON
  (we nudge the model with a strict reminder and try again).

The function deliberately keeps prompts specific — the LLM is told to reuse
identifiers that appear in the code and to avoid generic filler like
"this function does X" — so downstream consumers (knowledge map, search,
agent context) get signal rather than marketing prose.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any, Final, Optional

from botocore.exceptions import ClientError

from app.bedrock_runtime import get_bedrock_runtime
from app.config import get_settings, resolve_bedrock_text_model_id_for_region
from app.logging import get_logger

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

_ALLOWED_TYPES: Final[tuple[str, ...]] = ("function", "class", "api", "utility")
_DEFAULT_TYPE: Final[str] = "utility"

# Hard cap on chunk code we send to the LLM; anything larger is truncated
# with a visible marker so the model does not hallucinate continuations.
_MAX_CODE_CHARS: Final[int] = 6000

_DEFAULT_MAX_TOKENS: Final[int] = 700
_DEFAULT_TEMPERATURE: Final[float] = 0.1
_DEFAULT_TOP_P: Final[float] = 1.0

# One "parse attempt" == one LLM round-trip + one JSON decode. We allow 2 so
# that a malformed first response (preamble, fences) gets one retry with a
# stricter reminder. The underlying _invoke_llm handles transient API retries
# separately, so the effective upper bound is (parse_retries * api_retries).
_PARSE_MAX_ATTEMPTS: Final[int] = 2

# Extra application-level retry multiplier on top of botocore's built-in.
_APP_RETRY_MULTIPLIER: Final[int] = 3

_RETRIABLE_BEDROCK_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "TooManyRequestsException",
        "InternalServerException",
        "ModelTimeoutException",
    }
)

_SYSTEM_PROMPT: Final[str] = (
    "You are a senior software engineer doing a focused code review of ONE\n"
    "code chunk (a slice of a file, not the whole file). You must return a\n"
    "single JSON object describing it, with EXACTLY this shape:\n"
    "{\n"
    '  "summary": string,         // <= 2 sentences. Specific. Name the real\n'
    "                             //   identifiers, tables, endpoints, etc.\n"
    '  "purpose": string,         // <= 1 sentence. *Why* this code exists,\n'
    "                             //   not a restatement of *what* it does.\n"
    '  "inputs": [string, ...],   // parameters, request fields, env vars,\n'
    "                             //   message payload keys. [] if none.\n"
    '  "outputs": [string, ...],  // return values, response shape, emitted\n'
    "                             //   events, side effects. [] if none.\n"
    '  "dependencies": [string, ...], // imports, helpers called, DB tables,\n'
    "                             //   HTTP services, SDKs. [] if none.\n"
    '  "type": "function" | "class" | "api" | "utility"\n'
    "}\n"
    "Hard rules:\n"
    "- Output ONLY the JSON object. No prose, no markdown, no code fences.\n"
    "- Use identifiers that actually appear in the code. Do not invent names.\n"
    "- Be concrete. Reject generic phrases like 'this function does X',\n"
    "  'handles logic', 'processes data', 'utility helper'.\n"
    "- If a field truly has no content, use an empty list or empty string.\n"
    '- "type": "api" means it defines or dispatches an HTTP/RPC route;\n'
    '  "class" for class definitions; "function" for standalone callables;\n'
    '  "utility" only if it is genuinely a small shared helper.\n'
)

_STRICT_REMINDER: Final[str] = (
    "\n\nYour previous response was not valid JSON. Return ONLY the JSON\n"
    "object matching the schema above. No preamble. No code fences."
)

# --------------------------------------------------------------------------- #
# Model dispatch                                                              #
# --------------------------------------------------------------------------- #


def _is_anthropic(model_id: str) -> bool:
    m = model_id.lower()
    return "anthropic" in m or "claude" in m


def _is_meta_llama(model_id: str) -> bool:
    m = model_id.lower()
    return "meta" in m and "llama" in m


def _is_titan_text(model_id: str) -> bool:
    m = model_id.lower()
    return "titan" in m and "text" in m and "embed" not in m


def _invoke_anthropic(client: Any, model_id: str, system: str, user: str) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": _DEFAULT_MAX_TOKENS,
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


def _invoke_titan_text(client: Any, model_id: str, system: str, user: str) -> str:
    body = {
        "inputText": f"{system}\n\n{user}",
        "textGenerationConfig": {
            "maxTokenCount": _DEFAULT_MAX_TOKENS,
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


def _invoke_llama(client: Any, model_id: str, system: str, user: str) -> str:
    body = {
        "prompt": f"<|system|>\n{system}\n<|user|>\n{user}\n<|assistant|>\n",
        "max_gen_len": _DEFAULT_MAX_TOKENS,
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


def _dispatch(client: Any, model_id: str, system: str, user: str) -> str:
    if _is_anthropic(model_id):
        return _invoke_anthropic(client, model_id, system, user)
    if _is_meta_llama(model_id):
        return _invoke_llama(client, model_id, system, user)
    if _is_titan_text(model_id):
        return _invoke_titan_text(client, model_id, system, user)
    # Unknown provider: default to Anthropic messages (matches project default).
    log.info(
        "code_understanding: unknown model family; defaulting to Anthropic messages payload. model_id=%s",
        model_id,
    )
    return _invoke_anthropic(client, model_id, system, user)


# --------------------------------------------------------------------------- #
# Retrying invoke                                                             #
# --------------------------------------------------------------------------- #


def _invoke_llm(system: str, user: str, *, model_id: str) -> str:
    """Call Bedrock with application-level retry for transient errors."""
    client = get_bedrock_runtime()
    s = get_settings()
    max_attempts = max(1, s.bedrock.max_retries) * _APP_RETRY_MULTIPLIER
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        t0 = time.perf_counter()
        try:
            text = _dispatch(client, model_id, system, user)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            log.info(
                "code_understanding: LLM ok attempt=%s model_id=%s latency_ms=%.1f chars=%s",
                attempt,
                model_id,
                elapsed_ms,
                len(text),
            )
            return text
        except (ClientError, OSError, ValueError, json.JSONDecodeError) as e:
            last_error = e
            code = ""
            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
            retriable = code in _RETRIABLE_BEDROCK_ERROR_CODES
            if attempt < max_attempts and retriable:
                sleep_s = min(2.0 ** (attempt - 1) + random.random() * 0.25, 30.0)
                log.warning(
                    "code_understanding: LLM retry attempt=%s/%s code=%s sleep=%.2fs: %s",
                    attempt,
                    max_attempts,
                    code,
                    sleep_s,
                    e,
                )
                time.sleep(sleep_s)
                continue
            log.error(
                "code_understanding: LLM failed after %s attempt(s) model_id=%s code=%s: %s",
                attempt,
                model_id,
                code or "(none)",
                e,
            )
            raise
    # Defensive: loop always either returns or raises.
    if last_error:
        raise last_error
    raise RuntimeError("code_understanding: unreachable retry state")


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    # Drop the opening fence line (```json or ```).
    t = t.split("\n", 1)[1] if "\n" in t else ""
    # Drop the trailing fence if present.
    if "```" in t:
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _first_json_object(text: str) -> Optional[str]:
    """Return the first balanced ``{...}`` substring in ``text``, or None.

    Tolerates LLM preambles and trailing commentary by tracking brace depth
    while respecting quoted strings and escapes.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _as_str_list(val: object) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple)):
        out: list[str] = []
        for item in val:
            s = item.strip() if isinstance(item, str) else str(item).strip()
            if s:
                out.append(s)
        return out
    return []


def _coerce_type(val: object) -> str:
    if isinstance(val, str):
        t = val.strip().lower()
        if t in _ALLOWED_TYPES:
            return t
        if "class" in t:
            return "class"
        if "api" in t or "endpoint" in t or "route" in t or "handler" in t:
            return "api"
        if "function" in t or "method" in t:
            return "function"
    return _DEFAULT_TYPE


_GENERIC_MARKERS: Final[tuple[str, ...]] = (
    "this function does",
    "this class does",
    "this code does",
    "handles logic",
    "processes data",
    "utility helper",
)


def _looks_generic(summary: str) -> bool:
    s = summary.strip().lower()
    if not s:
        return True
    return any(m in s for m in _GENERIC_MARKERS)


def _parse_structured(text: str) -> dict:
    raw = _first_json_object(_strip_code_fences(text))
    if not raw:
        raise ValueError("no JSON object found in LLM response")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in LLM response: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("LLM JSON root must be an object")

    summary = str(obj.get("summary", "")).strip()
    purpose = str(obj.get("purpose", "")).strip()
    return {
        "summary": summary,
        "purpose": purpose,
        "inputs": _as_str_list(obj.get("inputs")),
        "outputs": _as_str_list(obj.get("outputs")),
        "dependencies": _as_str_list(obj.get("dependencies")),
        "type": _coerce_type(obj.get("type")),
    }


# --------------------------------------------------------------------------- #
# Prompt assembly                                                             #
# --------------------------------------------------------------------------- #


def _truncate_code(code: str) -> str:
    if len(code) <= _MAX_CODE_CHARS:
        return code
    head = code[: _MAX_CODE_CHARS - 80]
    return head + "\n/* ... truncated for analysis ... */\n"


def _build_user_message(chunk: dict) -> str:
    repo_id = str(chunk.get("repo_id") or "").strip() or "(unknown)"
    file = str(chunk.get("file") or "").strip() or "(unknown)"
    symbol = str(chunk.get("symbol") or "").strip() or "(anonymous)"
    language = str(chunk.get("language") or "").strip()
    start_line = chunk.get("start_line")
    end_line = chunk.get("end_line")
    code = _truncate_code(str(chunk.get("code") or ""))

    header = [
        f"repo_id: {repo_id}",
        f"file: {file}",
        f"symbol: {symbol}",
    ]
    if language:
        header.append(f"language: {language}")
    if isinstance(start_line, int) and isinstance(end_line, int):
        header.append(f"lines: {start_line}-{end_line}")

    return "\n".join(header) + "\n---\n" + code + "\n"


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def summarize_chunk(chunk: dict, *, model_id: Optional[str] = None) -> dict:
    """Return a structured description of ``chunk`` produced by a Bedrock LLM.

    Parameters
    ----------
    chunk:
        A dict with at least ``code``, ``file``, ``repo_id``, and ``symbol``.
        Extra fields (``language``, ``start_line``, ``end_line``) are used as
        additional hints for the LLM when present.
    model_id:
        Optional Bedrock text model id override (e.g. another Claude variant).
        Defaults to :attr:`BedrockSettings.model_id`.

    Returns
    -------
    dict
        ``{"summary", "purpose", "inputs", "outputs", "dependencies", "type"}``
        where lists are always ``list[str]`` (possibly empty) and ``type`` is
        one of ``"function" | "class" | "api" | "utility"``.

    Raises
    ------
    TypeError
        If ``chunk`` is not a dict.
    ValueError
        If ``chunk['code']`` is empty, or the model response cannot be parsed
        into structured JSON even after one strict-reminder retry.
    botocore.exceptions.ClientError
        If Bedrock fails with a non-retriable error, or after exhausting the
        retry budget.
    """
    if not isinstance(chunk, dict):
        raise TypeError("chunk must be a dict")
    code = str(chunk.get("code") or "").strip()
    if not code:
        raise ValueError("chunk['code'] must be non-empty")

    s = get_settings()
    mid = (model_id or "").strip() or s.bedrock.model_id
    mid = resolve_bedrock_text_model_id_for_region(mid, s.bedrock_region)
    base_user = _build_user_message({**chunk, "code": code})
    user = base_user

    t_all = time.perf_counter()
    last_parse_error: Exception | None = None

    for attempt in range(1, _PARSE_MAX_ATTEMPTS + 1):
        try:
            text = _invoke_llm(_SYSTEM_PROMPT, user, model_id=mid)
        except Exception:
            log.exception(
                "code_understanding: summarize_chunk LLM call failed repo_id=%s file=%s symbol=%s",
                chunk.get("repo_id"),
                chunk.get("file"),
                chunk.get("symbol"),
            )
            raise

        try:
            result = _parse_structured(text)
        except ValueError as e:
            last_parse_error = e
            log.warning(
                "code_understanding: parse attempt=%s/%s failed reason=%s raw_preview=%r",
                attempt,
                _PARSE_MAX_ATTEMPTS,
                e,
                text[:300],
            )
            if attempt < _PARSE_MAX_ATTEMPTS:
                user = base_user + _STRICT_REMINDER
                continue
            raise

        if _looks_generic(result["summary"]):
            log.info(
                "code_understanding: summary looked generic, accepting anyway repo_id=%s file=%s symbol=%s",
                chunk.get("repo_id"),
                chunk.get("file"),
                chunk.get("symbol"),
            )

        elapsed_ms = (time.perf_counter() - t_all) * 1000.0
        log.info(
            "code_understanding: summarize_chunk ok repo_id=%s file=%s symbol=%s type=%s "
            "inputs=%s outputs=%s deps=%s total_ms=%.1f",
            chunk.get("repo_id"),
            chunk.get("file"),
            chunk.get("symbol"),
            result["type"],
            len(result["inputs"]),
            len(result["outputs"]),
            len(result["dependencies"]),
            elapsed_ms,
        )
        return result

    # The loop either returns or raises; this is defensive only.
    if last_parse_error:
        raise last_parse_error
    raise RuntimeError("code_understanding: unreachable parse state")


__all__ = ["summarize_chunk"]
