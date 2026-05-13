"""Amazon Bedrock text embedding helpers (Titan, Cohere, etc.).

Uses :class:`app.config.BedrockSettings` for the embedding model id, region,
retries, and timeouts. Cohere embed models are invoked with multi-text
payloads; Titan-style models are invoked one text at a time (optionally
parallelized) so ``generate_embeddings_batch`` still offers batching and
amortized throughput.
"""

from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, cast

from botocore.exceptions import ClientError

from app.bedrock_runtime import get_bedrock_runtime
from app.config import get_settings
from app.logging import get_logger

log = get_logger(__name__)

# Cohere embed on Bedrock supports many texts; keep chunks conservative.
_COHERE_MAX_TEXTS_PER_REQUEST: int = 96

# Default parallel Titan invocations; callers may pass ``max_concurrency``.
_DEFAULT_MAX_CONCURRENCY: int = 5
# Hard cap to reduce accidental throttling
_MAX_WORKERS: int = 32

# Application-level retries (after botocore's built-in attempts)
_RETRIABLE_BEDROCK_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "TooManyRequestsException",
        "InternalServerException",
        "ModelTimeoutException",
    }
)

def _is_cohere_model(model_id: str) -> bool:
    m = model_id.lower()
    return "cohere" in m and "embed" in m


def _is_titan_model(model_id: str) -> bool:
    m = model_id.lower()
    return "titan" in m and "embed" in m


def _validate_non_empty(s: str, name: str) -> str:
    t = s.strip() if s else ""
    if not t:
        raise ValueError(f"{name} must be non-empty after strip()")
    return t


def _as_float_list(vec: object) -> list[float]:
    if not isinstance(vec, (list, tuple)):
        return [float(vec)]  # type: ignore[arg-type]
    return [float(x) for x in cast(list[object], vec)]


def _parse_cohere_embedding_payload(payload: object) -> list[list[float]]:
    """Normalise Cohere Bedrock body (``embeddings`` or ``embeddings.float``)."""
    if not isinstance(payload, dict):
        raise ValueError("Cohere response must be a JSON object")
    pl = cast(dict[str, object], payload)
    raw: object = pl.get("embeddings")
    if raw is None:
        raise ValueError("missing 'embeddings' in Cohere response")
    if isinstance(raw, dict) and "float" in raw:
        raw = raw["float"]
    if not isinstance(raw, list):
        raise ValueError("invalid Cohere embeddings type")
    if not raw:
        return []
    if not isinstance(raw[0], (list, tuple)):
        raw = [raw]  # single vector
    return [_as_float_list(x) for x in cast(list[object], raw)]


def _invoke_cohere_batch(
    client, model_id: str, texts: list[str], max_attempts: int
) -> tuple[list[list[float]], float]:
    body = {
        "texts": texts,
        "input_type": "search_document",
        "embedding_types": ["float"],
    }
    raw = json.dumps(body)
    t0 = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=raw.encode("utf-8"),
            )
            pl = json.loads(resp["body"].read())
            vectors = _parse_cohere_embedding_payload(pl)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return vectors, elapsed_ms
        except (ClientError, OSError, ValueError, json.JSONDecodeError) as e:
            last_error = e
            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
            else:
                code = ""
            sleep_s = min(2.0 ** (attempt - 1) + random.random() * 0.25, 30.0)
            retriable = code in _RETRIABLE_BEDROCK_ERROR_CODES
            if attempt < max_attempts and retriable:
                log.warning(
                    "embedding_service: Cohere batch retry (attempt %s/%s, code=%s) sleep=%.2fs: %s",
                    attempt,
                    max_attempts,
                    code,
                    sleep_s,
                    e,
                )
                time.sleep(sleep_s)
            else:
                log.error("embedding_service: Cohere batch failed after %s attempts: %s", attempt, e)
                raise
    if last_error:
        raise last_error
    raise RuntimeError("unreachable")


def _invoke_titan_one(
    client, model_id: str, text: str, max_attempts: int
) -> tuple[list[float], float]:
    body = json.dumps({"inputText": text})
    t0 = time.perf_counter()
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=body.encode("utf-8"),
            )
            pl = json.loads(resp["body"].read())
            emb = pl.get("embedding")
            if emb is None:
                raise ValueError("missing 'embedding' in Titan response")
            v = _as_float_list(emb)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return v, elapsed_ms
        except (ClientError, OSError, ValueError, json.JSONDecodeError) as e:
            if isinstance(e, ClientError):
                code = e.response.get("Error", {}).get("Code", "")
            else:
                code = ""
            sleep_s = min(2.0 ** (attempt - 1) + random.random() * 0.25, 30.0)
            retriable = code in _RETRIABLE_BEDROCK_ERROR_CODES
            if attempt < max_attempts and retriable:
                log.warning(
                    "embedding_service: Titan retry (attempt %s/%s, code=%s) sleep=%.2fs: %s",
                    attempt,
                    max_attempts,
                    code,
                    sleep_s,
                    e,
                )
                time.sleep(sleep_s)
            else:
                log.error("embedding_service: Titan invoke failed: %s", e)
                raise
    raise RuntimeError("unreachable")


def _embedding_model_id(override: Optional[str]) -> str:
    if override and override.strip():
        return override.strip()
    return get_settings().bedrock.embedding_model_id


def generate_embedding(text: str, *, model_id: Optional[str] = None) -> list[float]:
    """Return a single embedding vector (list of floats) for ``text``."""
    t = _validate_non_empty(text, "text")
    out = generate_embeddings_batch([t], model_id=model_id)
    if not out:
        raise RuntimeError("empty embedding result")
    return out[0]


def generate_embeddings_batch(
    texts: List[str],
    *,
    model_id: Optional[str] = None,
    max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
) -> List[list[float]]:
    """
    Return one vector per text, same order as ``texts``.

    * **Cohere** embed models: one or more Bedrock calls, each with up to
      :const:`_COHERE_MAX_TEXTS_PER_REQUEST` inputs.
    * **Titan**-style: one invoke per text; up to ``max_concurrency`` calls in
      parallel. Retries and latency are logged per sub-batch / worker.

    * ``model_id`` overrides :envvar:`BEDROCK_EMBEDDING_MODEL_ID` when set.
    """
    if not texts:
        return []
    cleaned: list[str] = []
    for i, s in enumerate(texts):
        try:
            cleaned.append(_validate_non_empty(s, f"texts[{i}]"))
        except ValueError as e:
            log.error("embedding_service: %s", e)
            raise

    mid = _embedding_model_id(model_id)
    client = get_bedrock_runtime()
    s = get_settings()
    app_retries = 3
    b_retries = max(1, s.bedrock.max_retries)

    if _is_cohere_model(mid):
        all_vecs: list[list[float]] = []
        t_batch0 = time.perf_counter()
        for i in range(0, len(cleaned), _COHERE_MAX_TEXTS_PER_REQUEST):
            chunk = cleaned[i : i + _COHERE_MAX_TEXTS_PER_REQUEST]
            try:
                vecs, ms = _invoke_cohere_batch(
                    client, mid, chunk, max_attempts=b_retries * app_retries
                )
            except Exception:
                log.exception("embedding_service: Cohere batch failed (model_id=%s)", mid)
                raise
            if len(vecs) != len(chunk):
                raise ValueError(
                    f"expected {len(chunk)} vectors from Bedrock, got {len(vecs)}"
                )
            all_vecs.extend(vecs)
            log.info(
                "embedding_service: cohere batch ok count=%s latency_ms=%.1f model_id=%s",
                len(chunk),
                ms,
                mid,
            )
        log.info(
            "embedding_service: cohere total texts=%s wall_ms=%.1f model_id=%s",
            len(cleaned),
            (time.perf_counter() - t_batch0) * 1000.0,
            mid,
        )
        return all_vecs

    if not _is_titan_model(mid) and not _is_cohere_model(mid):
        log.info(
            "embedding_service: model not recognised; using Titan-style single inputText. model_id=%s",
            mid,
        )

    results: list[Optional[list[float]]] = [None] * len(cleaned)
    max_workers = max(1, min(max_concurrency, len(cleaned), _MAX_WORKERS))
    t_batch0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs: dict[object, int] = {}
        for idx, tx in enumerate(cleaned):
            futs[ex.submit(_invoke_titan_one, client, mid, tx, b_retries * app_retries)] = idx
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                vec, ms = fut.result()
                results[idx] = vec
                log.debug(
                    "embedding_service: titan one ok index=%s latency_ms=%.1f model_id=%s",
                    idx,
                    ms,
                    mid,
                )
            except Exception as e:  # noqa: BLE001
                log.error(
                    "embedding_service: titan one failed index=%s model_id=%s: %s",
                    idx,
                    mid,
                    e,
                )
                raise
    out: list[list[float]] = [r for r in results if r is not None]
    if len(out) != len(cleaned):
        raise RuntimeError("incomplete titan batch results")
    log.info(
        "embedding_service: titan total texts=%s wall_ms=%.1f model_id=%s max_workers=%s",
        len(cleaned),
        (time.perf_counter() - t_batch0) * 1000.0,
        mid,
        max_workers,
    )
    return out


__all__ = [
    "generate_embedding",
    "generate_embeddings_batch",
]
