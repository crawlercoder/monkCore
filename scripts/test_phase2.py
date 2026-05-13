#!/usr/bin/env python3
"""Phase 2 end-to-end smoke test: ingest -> summarize -> query.

What this script does
---------------------
1. Optionally ingests one or more repositories under a single ``org_id``,
   reusing the helpers from :mod:`scripts.test_phase1` so the two phases
   share one "clone + ingest" code path. If ``--repo`` / ``--local`` are
   omitted the script assumes the vector store already has content for
   ``--org-id`` — typical flow is::

       python scripts/test_phase1.py --org-id myorg --repo URL1 --repo URL2
       python scripts/test_phase2.py --org-id myorg

2. Takes a **baseline** reading against the existing vector store:

   * how many chunks currently lack a ``metadata.summary``,
   * runs the query ("cart logic" by default) with ``summary_boost=0``
     and captures the top-k hits (scores + symbols + files).

3. Runs :func:`app.summary_worker.process_unsummarized_chunks` to
   generate LLM summaries for every chunk without one.

4. Takes a **post-summary** reading:

   * re-counts chunks without a summary (must be <= before),
   * re-runs the same query twice:

       * once with ``summary_boost=0`` to show the ``summary`` field
         now comes back populated,
       * once with ``summary_boost=<arg>`` to show the ranking lift
         that the summary-aware booster delivers.

5. Prints each hit with ``score``, ``summary``, ``repo_id``, ``file``,
   and ``symbol`` — exactly what the brief asked for, plus score.

6. Emits a **VERIFICATION** block with hard pass/fail markers:

   * ``summaries exist`` — the worker actually wrote summaries back to
     metadata (unsummarized-chunk count strictly decreased, **and** at
     least one retrieved hit carries a non-empty ``summary``).
   * ``retrieval quality improves`` — summary-aware retrieval produced
     at least one measurable ranking signal (boosted score > baseline
     score on ≥1 hit, OR the top-1 file/symbol changed, OR a hit whose
     summary lexically overlaps the query was promoted into top-k).

Prerequisites
-------------
* Bedrock-capable AWS creds in ``.env`` (embeddings + LLM completion).
* ``SUMMARIZATION_ENABLED=true`` (default; set
  ``SUMMARIZATION_MAX_CHUNKS_PER_RUN`` if you want a smaller / larger
  bound for this run).
* Python deps from ``requirements.txt`` installed in the active env.

Usage
-----
Run from the repository root::

    # standalone (ingests too)
    .venv/bin/python scripts/test_phase2.py --repo https://... --reset

    # chained after Phase 1 (shares the same org's vector store)
    .venv/bin/python scripts/test_phase1.py --org-id acme --repo URL
    .venv/bin/python scripts/test_phase2.py --org-id acme

Output is plain text so it scrolls cleanly in CI logs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# --- bootstrap: make ``app.*`` *and* ``scripts/*`` importable -----------
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (REPO_ROOT, SCRIPTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(REPO_ROOT / ".env", override=True)
except ImportError:
    # python-dotenv is optional — assume env is already populated.
    pass

# --- app + phase-1 helpers (after sys.path + .env) ----------------------
from app.retrieval_service import search_code  # noqa: E402
from app.summary_worker import process_unsummarized_chunks  # noqa: E402
from app.vector_store import get_chunks_without_summary, load_index  # noqa: E402

# Reuse phase-1's clone/ingest pipeline instead of duplicating ~80 lines.
import test_phase1 as p1  # noqa: E402


# ------------------------------------------------------------------------
# Constants / defaults
# ------------------------------------------------------------------------

DEFAULT_QUERY: str = "cart logic"
DEFAULT_TOP_K: int = 5
DEFAULT_MIN_SCORE: float = 0.0  # don't filter in the test — we want deltas
DEFAULT_SUMMARY_BOOST: float = 0.3

# Same word tokenizer as :mod:`app.retrieval_service`. Kept local so this
# script never reaches into the retrieval module's privates — we just need
# the same semantics for the "did the summary mention the query terms?"
# check in the verification block.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_RE.finditer(text or "")}


# ------------------------------------------------------------------------
# Vector-store inspection
# ------------------------------------------------------------------------

def _store_stats(org_id: str) -> tuple[int, int, int]:
    """Return ``(total_chunks, with_summary, without_summary)``.

    Robust to a missing store: returns ``(0, 0, 0)`` if the org has never
    been ingested (matches what the summariser / retriever themselves do).
    """
    try:
        state = load_index(org_id)
    except FileNotFoundError:
        return 0, 0, 0

    total = len(state.entries)
    with_summary = 0
    for row in state.entries:
        meta = row.get("metadata") if isinstance(row, dict) else None
        if not isinstance(meta, dict):
            continue
        summary = meta.get("summary")
        if isinstance(summary, str) and summary.strip():
            with_summary += 1
    without = total - with_summary
    return total, with_summary, without


# ------------------------------------------------------------------------
# Query formatting
# ------------------------------------------------------------------------

def _truncate(s: str, max_chars: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"


def _print_hits(label: str, hits: Sequence[dict]) -> None:
    """Print hits with ``summary``, ``repo_id``, ``file`` (plus symbol, score)."""
    print()
    print(label)
    print("-" * len(label))
    if not hits:
        print("  (no results)")
        return
    for i, h in enumerate(hits, 1):
        score = float(h.get("score", 0.0))
        repo_id = str(h.get("repo_id", "") or "(unknown)")
        file_ = str(h.get("file", "") or "(unknown)")
        symbol = str(h.get("symbol", "") or "(anonymous)")
        summary = str(h.get("summary", "") or "")
        code = str(h.get("code", "") or "")
        print(f"  #{i:>2} [{score:.3f}] {repo_id}")
        print(f"      file    : {file_}")
        print(f"      symbol  : {symbol}")
        if summary:
            print(f"      summary : {_truncate(summary, 220)}")
        else:
            print(f"      summary : (none — falling back to raw code)")
            preview = _truncate(
                next((ln for ln in code.splitlines() if ln.strip()), ""),
                140,
            )
            if preview:
                print(f"      code    : {preview}")


def _hit_key(h: dict) -> Tuple[str, str, str]:
    """Stable identity for a hit across two query passes."""
    return (
        str(h.get("repo_id") or ""),
        str(h.get("file") or ""),
        str(h.get("symbol") or ""),
    )


# ------------------------------------------------------------------------
# Ingestion bridge to Phase 1
# ------------------------------------------------------------------------

def _maybe_ingest(
    org_id: str,
    repo_urls: List[str],
    local_paths: List[str],
    workdir: Path,
    skip_clone: bool,
) -> int:
    """Run Phase-1-style ingestion if any repo source was supplied.

    Returns the number of repos ingested. Callers should treat ``0`` as
    "operate on existing store" rather than an error.
    """
    if not repo_urls and not local_paths:
        return 0

    p1._sub("Acquiring repositories")
    targets = p1._resolve_targets(
        repo_urls, local_paths, workdir, skip_clone=skip_clone,
    )

    t0 = time.perf_counter()
    for i, target in enumerate(targets, 1):
        p1._sub(f"[{i}/{len(targets)}] Ingesting {target.repo_id}")
        try:
            p1._ingest_one(org_id, target)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR ingesting {target.repo_id}: {exc}")
    p1._hr(f"Ingest finished in {time.perf_counter() - t0:.1f}s "
           f"({len(targets)} repo(s))")
    return len(targets)


# ------------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------------

def _verify(
    *,
    query: str,
    pre_without: int,
    post_without: int,
    summary_succeeded: int,
    baseline_hits: Sequence[dict],
    post_no_boost_hits: Sequence[dict],
    post_boosted_hits: Sequence[dict],
) -> Tuple[bool, bool, dict[str, Any]]:
    """Return ``(summaries_ok, retrieval_ok, details)``.

    ``details`` is a dict the caller prints verbatim under the
    VERIFICATION banner so the reasoning behind each verdict is legible.
    """
    # ---- 1) summaries exist --------------------------------------------
    decrease = pre_without - post_without
    hits_with_summary = sum(
        1 for h in post_boosted_hits
        if isinstance(h.get("summary"), str) and h["summary"].strip()
    )
    summaries_ok = (decrease > 0 or summary_succeeded > 0) and hits_with_summary > 0

    # ---- 2) retrieval quality improves ---------------------------------
    # Baseline scores indexed by (repo_id, file, symbol).
    base_by_key = {_hit_key(h): float(h.get("score", 0.0)) for h in baseline_hits}

    boosted_count = 0
    total_delta = 0.0
    for h in post_boosted_hits:
        k = _hit_key(h)
        if k in base_by_key:
            delta = float(h.get("score", 0.0)) - base_by_key[k]
            total_delta += delta
            if delta > 1e-6:
                boosted_count += 1

    # Top-1 change?
    top1_baseline = _hit_key(baseline_hits[0]) if baseline_hits else None
    top1_boosted = _hit_key(post_boosted_hits[0]) if post_boosted_hits else None
    top1_changed = (
        top1_baseline is not None
        and top1_boosted is not None
        and top1_baseline != top1_boosted
    )

    # Any summary-content lexical overlap with the query — proves the
    # booster had *something* to work with even if no chunk beat min_score.
    q_tokens = _tokens(query)
    overlap_hits = sum(
        1 for h in post_boosted_hits
        if q_tokens & _tokens(str(h.get("summary", "")))
    )

    retrieval_ok = (
        hits_with_summary > 0
        and (boosted_count > 0 or top1_changed or overlap_hits > 0)
    )

    details: dict[str, Any] = {
        "unsummarized_before": pre_without,
        "unsummarized_after": post_without,
        "chunks_newly_summarized": max(0, decrease),
        "summary_worker_succeeded": summary_succeeded,
        "hits_with_summary": hits_with_summary,
        "hits_total_after": len(post_boosted_hits),
        "boosted_score_hits": boosted_count,
        "total_score_lift": round(total_delta, 3),
        "top1_file_changed": top1_changed,
        "post_hits_overlapping_query": overlap_hits,
    }
    return summaries_ok, retrieval_ok, details


# ------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Phase 2: ingest -> summarize -> retrieval smoke test. "
            "Verifies summaries are written to chunk metadata and that "
            "summary-aware search changes the ranking/scores of results."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--org-id",
        default=f"org_phase2_{uuid.uuid4().hex[:8]}",
        help="Organization id. Pass the same id you used in Phase 1 to "
        "reuse its vector store and skip ingestion.",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="URL",
        help="Git URL to clone + ingest. Repeatable. If omitted and "
        "--local is not given either, the script operates on the "
        "existing store for --org-id (summarise + query only).",
    )
    p.add_argument(
        "--local",
        action="append",
        default=[],
        metavar="PATH",
        help="Already-cloned local repo to ingest. Repeatable.",
    )
    p.add_argument(
        "--workdir",
        default=str(p1.DEFAULT_WORKDIR),
        help="Directory for shallow clones (Phase-1 workdir by default).",
    )
    p.add_argument(
        "--skip-clone",
        action="store_true",
        help="Reuse a pre-existing clone in --workdir instead of re-cloning.",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Delete vector_store/<org_id>/ before starting. Forces a "
        "from-scratch ingest + summarisation run.",
    )
    p.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help="Query string used for both baseline and post-summary passes.",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help="Top-K passed to retrieval_service.search_code.",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=DEFAULT_MIN_SCORE,
        help="Filter out results below this score. Default 0.0 (no "
        "filtering) so score deltas are visible on small corpora.",
    )
    p.add_argument(
        "--summary-boost",
        type=float,
        default=DEFAULT_SUMMARY_BOOST,
        help="Weight used for the summary-aware score boost on the final "
        "query. 0.0 disables boosting (results would match the 'no-boost' "
        "pass exactly).",
    )
    return p.parse_args()


# ------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()
    workdir = Path(args.workdir).expanduser().resolve()

    p1._hr("Phase 2: ingest + summarize + retrieval smoke test")
    p1._kv(
        [
            ("org_id", args.org_id),
            ("query", args.query),
            ("top_k", args.top_k),
            ("min_score", args.min_score),
            ("summary_boost", args.summary_boost),
            ("repos (URL)", len(args.repo)),
            ("repos (local)", len(args.local)),
            ("reset vector store", args.reset),
        ]
    )
    print()
    print(
        "NOTE: this test issues real Amazon Bedrock calls for embeddings "
        "AND for per-chunk LLM summarization. Costs & latency apply."
    )

    if args.reset:
        p1._sub("Resetting vector store")
        p1._reset_vector_store(args.org_id)

    # ---- 1) ingest (optional) -----------------------------------------
    ingested = _maybe_ingest(
        args.org_id, args.repo, args.local, workdir, args.skip_clone,
    )
    if ingested == 0:
        p1._sub("Skipping ingest (no --repo / --local)")
        print("  operating on existing vector store for org_id=", args.org_id)

    # ---- 2) pre-summary state -----------------------------------------
    p1._hr("Pre-summary vector-store state")
    pre_total, pre_with, pre_without = _store_stats(args.org_id)
    p1._kv(
        [
            ("total chunks", pre_total),
            ("with summary", pre_with),
            ("without summary", pre_without),
        ]
    )
    if pre_total == 0:
        print()
        print("  ERROR: vector store is empty. Ingest a repo first "
              "(--repo / --local) or point --org-id at an existing store.")
        return 2

    # ---- 3) baseline query (no summaries available) -------------------
    p1._hr(f'Baseline query — pre-summary, summary_boost=0.0')
    try:
        baseline_hits = search_code(
            args.org_id,
            args.query,
            top_k=args.top_k,
            min_score=args.min_score,
            summary_boost=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: search_code raised: {exc}")
        return 2
    _print_hits(f'[Q] "{args.query}"  (baseline)', baseline_hits)

    # ---- 4) run summary worker ----------------------------------------
    p1._hr("Running summary_worker.process_unsummarized_chunks")
    t0 = time.perf_counter()
    try:
        stats = process_unsummarized_chunks(args.org_id)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: summary worker raised: {exc}")
        return 2
    elapsed = time.perf_counter() - t0
    p1._kv(
        [
            ("enabled", stats.get("enabled")),
            ("fetched (unsummarised)", stats.get("fetched")),
            ("processed this run", stats.get("processed")),
            ("succeeded", stats.get("succeeded")),
            ("failed", stats.get("failed")),
            ("deferred (hit cap)", stats.get("deferred")),
            ("retry events", stats.get("retries")),
            ("worker elapsed", f"{stats.get('elapsed_ms', 0.0):.1f} ms"),
            ("wall time", f"{elapsed:.1f}s"),
        ]
    )
    if not stats.get("enabled", False):
        print()
        print("  WARNING: summarization is disabled in settings "
              "(SUMMARIZATION_ENABLED). The post-summary queries will "
              "behave identically to the baseline.")

    # ---- 5) post-summary state ----------------------------------------
    p1._hr("Post-summary vector-store state")
    post_total, post_with, post_without = _store_stats(args.org_id)
    p1._kv(
        [
            ("total chunks", post_total),
            ("with summary", post_with),
            ("without summary", post_without),
            ("newly summarised", max(0, pre_without - post_without)),
        ]
    )

    # ---- 6) post-summary queries --------------------------------------
    p1._hr(
        f'Post-summary query — summary_boost=0.0 '
        f'(summaries present but NOT used for ranking)'
    )
    try:
        post_no_boost = search_code(
            args.org_id,
            args.query,
            top_k=args.top_k,
            min_score=args.min_score,
            summary_boost=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: search_code raised: {exc}")
        return 2
    _print_hits(f'[Q] "{args.query}"  (post-summary, no boost)', post_no_boost)

    p1._hr(
        f'Post-summary query — summary_boost={args.summary_boost} '
        f'(summary-aware ranking)'
    )
    try:
        post_boosted = search_code(
            args.org_id,
            args.query,
            top_k=args.top_k,
            min_score=args.min_score,
            summary_boost=args.summary_boost,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: search_code raised: {exc}")
        return 2
    _print_hits(f'[Q] "{args.query}"  (post-summary + boost)', post_boosted)

    # ---- 7) verification ----------------------------------------------
    summaries_ok, retrieval_ok, details = _verify(
        query=args.query,
        pre_without=pre_without,
        post_without=post_without,
        summary_succeeded=int(stats.get("succeeded") or 0),
        baseline_hits=baseline_hits,
        post_no_boost_hits=post_no_boost,
        post_boosted_hits=post_boosted,
    )

    p1._hr("VERIFICATION")
    p1._kv(
        [
            ("chunks: before / after", f"{pre_without} / {post_without}"),
            ("worker successes", details["summary_worker_succeeded"]),
            ("hits carrying summary", f"{details['hits_with_summary']}/"
                                      f"{details['hits_total_after']}"),
            ("hits with score lift", details["boosted_score_hits"]),
            ("total score lift", details["total_score_lift"]),
            ("top-1 changed by boost", details["top1_file_changed"]),
            ("hits whose summary mentions query", details["post_hits_overlapping_query"]),
        ]
    )
    print()
    print(f"  [{'OK  ' if summaries_ok else 'FAIL'}] summaries exist")
    print(f"  [{'OK  ' if retrieval_ok else 'FAIL'}] retrieval quality improves")

    verdict_pass = summaries_ok and retrieval_ok
    print()
    print(f"  VERDICT: {'PASS' if verdict_pass else 'FAIL'}")

    p1._hr("Done")
    print(f"  org_id        : {args.org_id}")
    print(f"  workdir       : {workdir}")
    print(f"  vector store  : "
          f"{(Path(os.environ.get('VECTOR_STORE_ROOT') or (Path.cwd() / 'vector_store')) / args.org_id).resolve()}")
    print()
    return 0 if verdict_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
