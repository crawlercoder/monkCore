#!/usr/bin/env python3
"""Phase 1 end-to-end smoke test: ingest 2-3 repos, then search.

What this script does
---------------------
1. Clones (or reuses) 2-3 small public repositories under a single
   ``org_id``.
2. Runs :func:`app.ingestion_pipeline.ingest_repo` on each and prints
   per-repo + total stats — files processed, chunks created, chunks
   inserted, chunks skipped (the FAISS-level dedup side of the
   contract).
3. Optionally re-ingests the first repo to demonstrate that the
   per-chunk ``chunk_hash`` dedup actually skips instead of
   double-inserting (``--no-dedup-probe`` to disable).
4. Runs one or more search queries via
   :func:`app.retrieval_service.search_code` — defaults to the
   brief's ``"cart logic"`` and ``"API controller"`` — and prints the
   top-k hits with ``repo_id``, ``file``, ``symbol``, ``score``, and
   a preview of the matched code.

Prerequisites
-------------
* Working ``.env`` at repo root with AWS credentials that can call
  Bedrock (embedding model) — the script will issue real Bedrock
  requests. See :mod:`app.config`.
* Network access for ``git clone`` on first run (or pass
  ``--local PATH`` / ``--skip-clone``).
* Python deps from ``requirements.txt`` installed in the active env.

Usage
-----
Run from the repository root:

.. code-block:: bash

    .venv/bin/python scripts/test_phase1.py                # default repos
    .venv/bin/python scripts/test_phase1.py --reset        # wipe vector_store/<org>/ first
    .venv/bin/python scripts/test_phase1.py \\
        --repo https://github.com/pallets/click \\
        --repo https://github.com/expressjs/express \\
        --query "cart logic" --query "API controller"

Output is plain text, one logical section at a time, so it scrolls
cleanly in CI logs and terminals without color support.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# --- bootstrap: make ``app.*`` importable and load .env -----------------
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(REPO_ROOT / ".env", override=True)
except ImportError:
    # python-dotenv missing is fine — assume env is already populated.
    pass

# --- app imports (after sys.path + .env) --------------------------------
from app.ingestion_pipeline import ingest_repo  # noqa: E402
from app.retrieval_service import search_code  # noqa: E402


# ------------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------------

# Three small, well-known public repos that together cover Java, JS, and
# Python and collectively contain tokens like "controller", "route", and
# enough structural code to exercise the chunker on all three parsers.
# Users can override with --repo; see argparse below.
DEFAULT_REPOS: Tuple[str, ...] = (
    "https://github.com/spring-guides/gs-rest-service",
    "https://github.com/expressjs/express",
    "https://github.com/pallets/click",
)

DEFAULT_QUERIES: Tuple[str, ...] = ("cart logic", "API controller")

DEFAULT_WORKDIR = REPO_ROOT / ".phase1_tmp"


@dataclass
class RepoTarget:
    """One repository to ingest in this run."""

    repo_id: str
    repo_path: Path
    source: str  # URL we cloned from, or "local" if provided via --local
    cloned_here: bool  # True if *this* script cloned it (so we may clean up)


# ------------------------------------------------------------------------
# Printing helpers — plain text, no color, no external deps
# ------------------------------------------------------------------------

_HR = "=" * 78
_SUB = "-" * 78


def _hr(title: str = "") -> None:
    print()
    print(_HR)
    if title:
        print(f"  {title}")
        print(_HR)


def _sub(title: str) -> None:
    print()
    print(_SUB)
    print(f"  {title}")
    print(_SUB)


def _kv(rows: List[Tuple[str, object]], indent: int = 2) -> None:
    """Print key: value pairs aligned on the longest key."""
    if not rows:
        return
    width = max(len(k) for k, _ in rows)
    pad = " " * indent
    for k, v in rows:
        print(f"{pad}{k.ljust(width)} : {v}")


# ------------------------------------------------------------------------
# Repo acquisition
# ------------------------------------------------------------------------

def _repo_id_from_url(url: str) -> str:
    """Derive a stable, filesystem-safe repo_id from a git URL.

    We don't use the registry's repo_id format here because this test
    script talks to ingest_repo/search_code directly — no DynamoDB. The
    only constraint is that the id is stable and readable in the output.
    """
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    # Slug-ify: keep ascii alnum + _- only.
    slug = "".join(c if (c.isalnum() or c in "_-") else "_" for c in name.lower())
    return f"repo_{slug}" if slug else f"repo_{uuid.uuid4().hex[:8]}"


def _clone_repo(url: str, dest: Path, *, skip_if_exists: bool) -> None:
    """Shallow-clone ``url`` to ``dest``; overwrite unless skip_if_exists."""
    if dest.exists():
        if skip_if_exists:
            print(f"      reuse existing clone at {dest}")
            return
        print(f"      remove existing: {dest}")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    r = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(
            f"git clone failed ({url}): {r.stderr.strip()[:500]}"
        )
    print(f"      cloned in {elapsed:.1f}s -> {dest}")


def _resolve_targets(
    repo_urls: List[str],
    local_paths: List[str],
    workdir: Path,
    *,
    skip_clone: bool,
) -> List[RepoTarget]:
    """Turn --repo / --local args into concrete on-disk targets."""
    workdir.mkdir(parents=True, exist_ok=True)
    targets: List[RepoTarget] = []

    for url in repo_urls:
        repo_id = _repo_id_from_url(url)
        dest = workdir / repo_id
        _clone_repo(url, dest, skip_if_exists=skip_clone)
        targets.append(
            RepoTarget(
                repo_id=repo_id, repo_path=dest, source=url, cloned_here=True,
            )
        )

    for raw in local_paths:
        p = Path(raw).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"--local path is not a directory: {p}")
        repo_id = f"repo_{p.name}"
        targets.append(
            RepoTarget(
                repo_id=repo_id, repo_path=p, source="local", cloned_here=False,
            )
        )

    if not targets:
        raise SystemExit("No repos to ingest (check --repo / --local flags).")
    return targets


# ------------------------------------------------------------------------
# Ingestion + totals
# ------------------------------------------------------------------------

def _ingest_one(org_id: str, target: RepoTarget) -> dict:
    """Ingest one repo and print per-repo stats. Returns the pipeline dict."""
    t0 = time.perf_counter()
    result = ingest_repo(org_id, target.repo_id, str(target.repo_path))
    elapsed = time.perf_counter() - t0

    created = int(result.get("chunks_created", 0))
    inserted = int(result.get("chunks_inserted", 0))
    skipped = max(0, created - inserted)

    _kv(
        [
            ("repo_id", target.repo_id),
            ("source", target.source),
            ("path", target.repo_path),
            ("files_processed", result.get("files_processed", 0)),
            ("chunks_created", created),
            ("chunks_inserted", inserted),
            ("chunks_skipped", skipped),
            ("elapsed", f"{elapsed:.1f}s"),
        ],
        indent=6,
    )
    # Keep a normalized view for downstream aggregation.
    return {
        "files_processed": int(result.get("files_processed", 0)),
        "chunks_created": created,
        "chunks_inserted": inserted,
        "chunks_skipped": skipped,
    }


def _reset_vector_store(org_id: str) -> None:
    """Delete ``vector_store/<org_id>/`` so re-runs start from an empty index."""
    base = Path(os.environ.get("VECTOR_STORE_ROOT", "")).expanduser() or (
        Path.cwd() / "vector_store"
    )
    target = (base / org_id).resolve()
    if target.is_dir():
        print(f"      removing {target}")
        shutil.rmtree(target)
    else:
        print(f"      (no existing store at {target})")


# ------------------------------------------------------------------------
# Query formatting
# ------------------------------------------------------------------------

def _code_preview(code: str, max_chars: int = 160) -> str:
    """First non-empty line of ``code``, clipped to ``max_chars``."""
    for line in (code or "").splitlines():
        stripped = line.strip()
        if stripped:
            return (
                stripped[: max_chars - 1] + "…"
                if len(stripped) > max_chars
                else stripped
            )
    return "(empty)"


def _print_query_results(query: str, hits: List[dict]) -> None:
    print()
    header = f'[Q] "{query}"'
    print(header)
    print("-" * len(header))
    if not hits:
        print("  (no results above min_score)")
        return
    for i, h in enumerate(hits, 1):
        score = float(h.get("score", 0.0))
        repo_id = str(h.get("repo_id", ""))
        file_ = str(h.get("file", ""))
        symbol = str(h.get("symbol", "")) or "(anonymous)"
        code = _code_preview(str(h.get("code", "")))
        print(
            f"  #{i:>2} [{score:.3f}] {repo_id}"
            f"  {file_}  ::  {symbol}"
        )
        print(f"          | {code}")


# ------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 1: ingest 2-3 repos into one org and query them.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--org-id",
        default=f"org_phase1_{uuid.uuid4().hex[:8]}",
        help="Organization id. A unique one is auto-generated by default so "
        "re-runs don't collide with earlier FAISS stores.",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="URL",
        help="Git URL to clone and ingest. Repeatable. If neither --repo "
        "nor --local is given, three small public defaults are used.",
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
        default=str(DEFAULT_WORKDIR),
        help="Directory for shallow clones.",
    )
    p.add_argument(
        "--skip-clone",
        action="store_true",
        help="If a clone target already exists in --workdir, reuse it "
        "instead of re-cloning.",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="Delete vector_store/<org_id>/ before starting. Makes "
        "'chunks_inserted' meaningful on repeat runs.",
    )
    p.add_argument(
        "--no-dedup-probe",
        action="store_true",
        help="Skip the 're-ingest first repo' step that demonstrates "
        "chunk_hash dedup (all chunks skipped on the second pass).",
    )
    p.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query string. Repeatable. Defaults to "
        '"cart logic" and "API controller" if unset.',
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Top-K passed to retrieval_service.search_code.",
    )
    p.add_argument(
        "--min-score",
        type=float,
        default=0.25,
        help="Filter out results with score below this threshold "
        "(0.0 disables filtering).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    repo_urls = args.repo or (list(DEFAULT_REPOS) if not args.local else [])
    queries: List[str] = args.query or list(DEFAULT_QUERIES)
    workdir = Path(args.workdir).expanduser().resolve()

    _hr("Phase 1: ingest + search smoke test")
    _kv(
        [
            ("org_id", args.org_id),
            ("workdir", workdir),
            ("repos (URL)", len(repo_urls)),
            ("repos (local)", len(args.local)),
            ("queries", queries),
            ("top_k", args.top_k),
            ("min_score", args.min_score),
            ("reset vector store", args.reset),
        ]
    )
    print()
    print(
        "NOTE: ingestion issues real Amazon Bedrock embedding calls. "
        "Costs & latency will apply."
    )

    if args.reset:
        _sub("Resetting vector store")
        _reset_vector_store(args.org_id)

    # 1) Resolve (clone) targets
    _sub("Acquiring repositories")
    try:
        targets = _resolve_targets(
            repo_urls,
            args.local,
            workdir,
            skip_clone=args.skip_clone,
        )
    except Exception as exc:
        print(f"  ERROR resolving repos: {exc}")
        return 2

    # 2) Ingest each, accumulating totals
    totals = {
        "files_processed": 0,
        "chunks_created": 0,
        "chunks_inserted": 0,
        "chunks_skipped": 0,
    }
    per_repo_results: List[Tuple[RepoTarget, dict]] = []
    t_ingest_all = time.perf_counter()

    for i, target in enumerate(targets, 1):
        _sub(f"[{i}/{len(targets)}] Ingesting {target.repo_id}")
        try:
            stats = _ingest_one(args.org_id, target)
        except Exception as exc:
            # Surface the failure but continue to remaining repos so the
            # rest of the pipeline still exercises.
            print(f"  ERROR ingesting {target.repo_id}: {exc}")
            stats = {
                "files_processed": 0,
                "chunks_created": 0,
                "chunks_inserted": 0,
                "chunks_skipped": 0,
            }
        per_repo_results.append((target, stats))
        for k, v in stats.items():
            totals[k] += int(v)

    ingest_elapsed = time.perf_counter() - t_ingest_all

    _hr(f"Totals across {len(targets)} repo(s)")
    _kv(
        [
            ("files_processed", totals["files_processed"]),
            ("chunks_created", totals["chunks_created"]),
            ("chunks_inserted (new)", totals["chunks_inserted"]),
            ("chunks_skipped (dedup)", totals["chunks_skipped"]),
            ("total ingest time", f"{ingest_elapsed:.1f}s"),
        ]
    )

    # 3) Optional idempotency probe: re-ingest the first repo
    if not args.no_dedup_probe and targets:
        probe = targets[0]
        _sub(f"Idempotency probe: re-ingesting {probe.repo_id}")
        try:
            stats = _ingest_one(args.org_id, probe)
            if stats["chunks_inserted"] == 0 and stats["chunks_skipped"] > 0:
                print("      ✔ dedup works — every chunk was skipped")
            elif stats["chunks_created"] == 0:
                print("      (empty repo — probe inconclusive)")
            else:
                print(
                    "      ! unexpected: some chunks were inserted on "
                    "the second pass (expected all skipped)"
                )
        except Exception as exc:
            print(f"  ERROR during probe: {exc}")

    # 4) Queries
    _hr(
        f"Query results (top_k={args.top_k}, min_score={args.min_score})"
    )
    for q in queries:
        try:
            hits = search_code(
                args.org_id,
                q,
                top_k=args.top_k,
                min_score=args.min_score,
            )
        except Exception as exc:
            print()
            print(f'[Q] "{q}"   ERROR: {exc}')
            continue
        _print_query_results(q, hits)

    _hr("Done")
    print(f"  org_id        : {args.org_id}")
    print(f"  workdir       : {workdir}")
    print(f"  vector store  : "
          f"{(Path(os.environ.get('VECTOR_STORE_ROOT') or (Path.cwd() / 'vector_store')) / args.org_id).resolve()}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
