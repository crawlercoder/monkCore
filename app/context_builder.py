"""Build compact RAG context from a natural-language spec (no LLM calls).

Uses :func:`app.retrieval_service.search_code` — embeddings + vector search
only — to pull the top chunks for the org. The **only** string sent into the
embedding / search pipeline is the caller's ``spec`` (after strip); there is
no hardcoded or alternative query.
"""

from __future__ import annotations

from typing import Final, TypedDict

from app.logging import get_logger, log_event
from app.retrieval_service import search_code

log = get_logger(__name__)

# Pull a moderate top-k from FAISS; we filter further below.
_SEARCH_TOP_K: Final[int] = 12
# Drop low-similarity hits at the retrieval layer; a 0.25 floor strips the
# clearly-unrelated tail without starving small indexes.
_SEARCH_MIN_SCORE: Final[float] = 0.25

# Cap the per-call context after filtering. Five strong chunks reduce the
# chance of irrelevant snippets confusing downstream prompting.
_CONTEXT_MAX_CHUNKS: Final[int] = 5

# Drop snippets that are too short to carry meaningful signal.
_MIN_SNIPPET_CHARS: Final[int] = 20

# Keep each ``code`` field small; total context stays bounded for downstream LLMs.
_MAX_CODE_CHARS: Final[int] = 2000
_TRUNC_MARKER: Final[str] = "\n/* … truncated … */\n"


def _truncate_code(code: str) -> str:
    if len(code) <= _MAX_CODE_CHARS:
        return code
    head_len = _MAX_CODE_CHARS - len(_TRUNC_MARKER) - 32
    if head_len < 256:
        head_len = 256
    return code[:head_len] + _TRUNC_MARKER


class _ContextItem(TypedDict):
    repo_id: str
    file: str
    code: str


class _ContextBundle(TypedDict):
    spec: str
    context: list[_ContextItem]


def build_context(org_id: str, spec: str) -> _ContextBundle:
    """Return the spec plus up to :data:`_CONTEXT_MAX_CHUNKS` chunks for ``org_id``.

    Retrieval uses **only** the stripped ``spec`` string as the embed/query
    passed to :func:`~app.retrieval_service.search_code` — there are no
    hardcoded query strings, no paraphrases, and no second hidden query.

    Steps:

    1. Set ``query = spec.strip()`` and pass ``query`` to ``search_code`` (that
       function embeds ``query`` and runs vector search) with
       ``top_k=_SEARCH_TOP_K`` and ``min_score=_SEARCH_MIN_SCORE``.
    2. Drop noisy rows: empty snippets, snippets shorter than
       :data:`_MIN_SNIPPET_CHARS`, and exact duplicates of an already-kept
       snippet.
    3. Take up to :data:`_CONTEXT_MAX_CHUNKS` rows, best score first.
    4. For each row, keep only ``repo_id``, ``file``, and ``code`` (truncated
       to :data:`_MAX_CODE_CHARS`).

    No LLM is invoked. Empty or missing chunks after filtering yield a shorter
    ``context`` list (e.g. small or empty index).

    Parameters
    ----------
    org_id:
        Organization whose FAISS store is searched.
    spec:
        Natural-language spec; this exact text (after stripping) is the sole
        retrieval query for embeddings + FAISS.

    Returns
    -------
    dict
        ``{"spec": <str>, "context": [{ "repo_id", "file", "code" }, ...]}``

    Raises
    ------
    ValueError
        If ``spec`` is empty or ``org_id`` is empty.
    """
    oid = (org_id or "").strip()
    if not oid:
        raise ValueError("org_id must be non-empty")
    # Single source of truth for vector search: the job spec string only.
    query = (spec or "").strip()
    if not query:
        raise ValueError("spec must be non-empty")

    rows = search_code(
        oid,
        query,
        top_k=_SEARCH_TOP_K,
        min_score=_SEARCH_MIN_SCORE,
    )
    original_count = len(rows)

    # Drop counters: report dropped chunks by reason so retrieval quality
    # regressions (e.g. a corpus full of stub files) are visible in logs.
    dropped = {"empty": 0, "too_short": 0, "duplicate": 0, "over_cap": 0}

    out: list[_ContextItem] = []
    seen_codes: set[str] = set()
    for r in rows:
        code = (r.get("code") or "").strip()
        if not code:
            dropped["empty"] += 1
            continue
        if len(code) < _MIN_SNIPPET_CHARS:
            dropped["too_short"] += 1
            continue
        if code in seen_codes:
            dropped["duplicate"] += 1
            continue
        if len(out) >= _CONTEXT_MAX_CHUNKS:
            dropped["over_cap"] += 1
            continue
        seen_codes.add(code)
        out.append(
            {
                "repo_id": str(r.get("repo_id", "")),
                "file": str(r.get("file", "")),
                "code": _truncate_code(code),
            }
        )

    log_event(
        log,
        "context_builder.retrieval",
        "context built",
        org_id=oid,
        query_chars=len(query),
        top_k=_SEARCH_TOP_K,
        min_score=_SEARCH_MIN_SCORE,
        original_count=original_count,
        filtered_count=len(out),
        dropped_empty=dropped["empty"],
        dropped_too_short=dropped["too_short"],
        dropped_duplicate=dropped["duplicate"],
        dropped_over_cap=dropped["over_cap"],
    )
    return {"spec": query, "context": out}


__all__ = ["build_context"]
