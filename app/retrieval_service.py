"""High-level ``search_code`` helper over Bedrock embeddings + FAISS store.

Results now surface the LLM-generated ``summary`` (produced by
:mod:`app.summary_worker` / :mod:`app.code_understanding`) alongside the raw
``code``. The output shape is strictly additive compared to the previous
version — existing consumers that read ``{repo_id, file, symbol, code, score}``
keep working; new consumers can prefer ``summary`` and fall back to ``code``
when it is empty.

An opt-in lexical boost (:data:`summary_boost`) can give a small bump to
results whose summary text overlaps with the query tokens. The feature is
off by default (``summary_boost=0.0``) to preserve the exact scoring
behaviour of the previous release.
"""

from __future__ import annotations

import re
import time
from typing import Any, Final, List, Optional

from app.embedding_service import generate_embedding
from app.logging import get_logger
from app.vector_store import search as _vector_search

log = get_logger(__name__)

_DEFAULT_TOP_K: Final[int] = 10
# Matches are kept when the *final* score (after optional summary boost) is
# ``>= _DEFAULT_MIN_SCORE``; see :func:`_l2_to_score` for the base mapping.
_DEFAULT_MIN_SCORE: Final[float] = 0.25

# Off by default — opt-in so legacy callers see identical rankings to before.
_DEFAULT_SUMMARY_BOOST: Final[float] = 0.0

# Word-ish tokens: start with a letter, then letters/digits/underscores,
# minimum length 3. Good enough to match identifier-shaped query terms like
# "cart", "add_to_cart", "api", "controller" without false-positives on
# noise words ("is", "of", "a").
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _l2_to_score(distance: float) -> float:
    """Map FAISS L2 distance to a bounded similarity score in [0, 1].

    FAISS IndexFlatL2 returns squared L2; values depend on vector magnitude.
    ``1 / (1 + d)`` is a common monotone, normalization-free mapping: identical
    vectors → ``1.0``, unrelated → approaches ``0.0``. This preserves ranking
    while giving callers a stable cutoff for :func:`search_code` ``min_score``.
    """
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if d < 0:
        d = 0.0
    return 1.0 / (1.0 + d)


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _overlap_ratio(query_tokens: set[str], summary: str) -> float:
    """Return ``|Q ∩ S| / |Q|`` in ``[0, 1]``.

    We divide by the query size (not the union) so that a short, on-topic
    summary can still match a focused query strongly without being penalised
    by irrelevant query words.
    """
    if not query_tokens or not summary:
        return 0.0
    s_tokens = _tokens(summary)
    if not s_tokens:
        return 0.0
    return len(query_tokens & s_tokens) / len(query_tokens)


def search_code(
    org_id: str,
    query: str,
    *,
    top_k: int = _DEFAULT_TOP_K,
    min_score: float = _DEFAULT_MIN_SCORE,
    model_id: Optional[str] = None,
    summary_boost: float = _DEFAULT_SUMMARY_BOOST,
) -> List[dict[str, Any]]:
    """
    Embed ``query`` with Bedrock and return the top-k nearest code chunks
    from this org's FAISS store, filtered by a similarity ``min_score``.

    Result shape (every key always present)::

        {
            "repo_id": str,
            "file":    str,
            "symbol":  str,
            "summary": str,    # LLM summary; "" when not yet generated
            "code":    str,    # raw chunk code; fallback when summary is empty
            "score":   float,
        }

    Parameters
    ----------
    top_k:
        Upstream ``vector_store.search`` is called with this value.
    min_score:
        ``0.0`` disables filtering. Values are in ``[0, 1]`` via
        ``1 / (1 + L2)`` (tweak per your embedding model). The filter runs
        on the *final* score — after any :paramref:`summary_boost` is applied.
    model_id:
        Override :envvar:`BEDROCK_EMBEDDING_MODEL_ID` for this call.
    summary_boost:
        Lexical-overlap boost applied when the query's tokens appear in the
        chunk's ``summary`` text. Clamped to ``[0, 1]``. The score becomes
        ``min(1, base + summary_boost * overlap_ratio)``. ``0.0`` (default)
        leaves scores untouched and preserves the legacy ranking behaviour.

    Notes
    -----
    * Backward compatibility: all previous keys are still emitted; ``summary``
      is strictly additive. Previous callers that only read
      ``{repo_id, file, symbol, code, score}`` keep working unchanged.
    * Summary fallback: when ``metadata["summary"]`` is absent or empty the
      ``summary`` field is an empty string — callers should display
      ``result["summary"] or result["code"]``.
    * ``code`` is read from ``metadata["code"]`` if indexed (see
      :mod:`app.code_chunker`); otherwise it is returned as an empty string
      with a warning log so callers know to hydrate from the repo snapshot.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query must be non-empty")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    try:
        boost = float(summary_boost)
    except (TypeError, ValueError):
        boost = 0.0
    # Clamp to a safe range so pathological values can't invert the scoring.
    boost = max(0.0, min(1.0, boost))

    t_all = time.perf_counter()

    t0 = time.perf_counter()
    try:
        emb = generate_embedding(q, model_id=model_id)
    except Exception:
        log.exception("retrieval_service: embedding failed org_id=%s", org_id)
        raise
    embed_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    try:
        hits = _vector_search(org_id, emb, top_k)
    except Exception:
        log.exception("retrieval_service: vector_store.search failed org_id=%s", org_id)
        raise
    search_ms = (time.perf_counter() - t0) * 1000.0

    # Only tokenise the query when boost actually matters — keeps the no-boost
    # path identical (and slightly cheaper) for legacy callers.
    q_tokens = _tokens(q) if boost > 0.0 else set()

    results: list[dict[str, Any]] = []
    missing_code = 0
    with_summary = 0
    boosted_count = 0

    for h in hits:
        meta = h.get("metadata", {})
        if not isinstance(meta, dict):
            meta = {}

        distance = float(h.get("distance", float("inf")))
        base_score = _l2_to_score(distance)

        summary_raw = meta.get("summary", "")
        if not isinstance(summary_raw, str):
            summary_raw = str(summary_raw)
        summary = summary_raw.strip()

        code_raw = meta.get("code", "")
        if not isinstance(code_raw, str):
            code_raw = str(code_raw)
        code = code_raw

        score = base_score
        if boost > 0.0 and summary:
            ratio = _overlap_ratio(q_tokens, summary)
            if ratio > 0.0:
                score = min(1.0, base_score + boost * ratio)
                boosted_count += 1

        if score < min_score:
            continue

        if summary:
            with_summary += 1
        if not code:
            missing_code += 1

        results.append(
            {
                "repo_id": str(meta.get("repo_id", "")),
                "file": str(meta.get("file", "")),
                "symbol": str(meta.get("symbol", "")),
                "summary": summary,
                "code": code,
                "score": round(score, 6),
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)

    total_ms = (time.perf_counter() - t_all) * 1000.0
    if missing_code:
        log.warning(
            "retrieval_service: %d result(s) have empty code metadata; "
            "index with metadata.code for full fidelity",
            missing_code,
        )
    log.info(
        "retrieval_service: search_code org_id=%s hits=%s kept=%s with_summary=%s "
        "boost=%.3f boosted=%s top_k=%s min_score=%.3f "
        "embed_ms=%.1f search_ms=%.1f total_ms=%.1f",
        org_id,
        len(hits),
        len(results),
        with_summary,
        boost,
        boosted_count,
        top_k,
        min_score,
        embed_ms,
        search_ms,
        total_ms,
    )
    return results


__all__ = ["search_code"]
