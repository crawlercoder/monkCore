"""Orchestrate scan → chunk → embed → FAISS-insert for a single repo.

Composes :mod:`app.repo_scanner`, :mod:`app.code_chunker`,
:mod:`app.embedding_service`, :mod:`app.vector_store`, and (filesystem)
:mod:`app.repo_initializer`. See :func:`ingest_repo` for the canonical flow.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Final, Optional

from app.code_chunker import chunk_code
from app.embedding_service import generate_embeddings_batch
from app.knowledge_map import generate_org_map
from app.logging import get_logger, log_context
from app.repo_initializer import merge_metadata
from app.repo_scanner import scan_repo
from app.vector_store import index_chunks, init_index, load_index, save_index

log = get_logger(__name__)

# Per-embedding-call text cap; keep it modest so one bad file cannot OOM boto.
_EMBED_BATCH_SIZE: Final[int] = 64
# Per-chunk code truncation before embedding (safety). Bedrock max tokens are model-specific.
_EMBED_TEXT_MAX_CHARS: Final[int] = 24_000
# Maximum chunks we will send in one pipeline run (protects against runaway repos)
_INGEST_MAX_CHUNKS: Final[int] = 100_000


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _truncate_for_embedding(text: str) -> str:
    if len(text) <= _EMBED_TEXT_MAX_CHARS:
        return text
    head = text[: _EMBED_TEXT_MAX_CHARS - 200]
    return head + "\n/* ... truncated for embedding ... */\n"


def _safe_init_or_load(org_id: str, dimension: Optional[int]):
    """Return the org state, initializing the FAISS store on first use."""
    try:
        return load_index(org_id)
    except FileNotFoundError:
        if dimension is None:
            init_index(org_id)
        else:
            init_index(org_id, dimension=dimension)
        return load_index(org_id)


def ingest_repo(
    org_id: str,
    repo_id: str,
    repo_path: str,
    *,
    embedding_model_id: Optional[str] = None,
) -> dict[str, int]:
    """
    Full ingestion pipeline for one repo on local disk.

    Steps
    -----
    1. **Scan** ``repo_path`` for supported source files (:mod:`app.repo_scanner`).
    2. **Chunk** each file with ``chunk_code`` (per-file errors are logged and
       skipped, not raised).
    3. **Embed** chunk ``code`` in batches of :data:`_EMBED_BATCH_SIZE` via
       :func:`app.embedding_service.generate_embeddings_batch`.
    4. **Insert** into the org's FAISS store via
       :func:`app.vector_store.index_chunks` (``chunk_hash`` de-duplication).
    5. **Persist** ``vector_store.save_index`` and mark the repo metadata
       ``status=INDEXED`` with ``last_indexed`` ISO-UTC timestamp.

    Returns
    -------
    ``{"files_processed": int, "chunks_created": int, "chunks_inserted": int}``
    """
    if not (org_id or "").strip() or not (repo_id or "").strip():
        raise ValueError("org_id and repo_id must be non-empty")
    if not (repo_path or "").strip():
        raise ValueError("repo_path must be non-empty")

    t_all = time.perf_counter()
    with log_context(org_id=org_id, repo_id=repo_id):
        log.info("ingestion_pipeline: start repo_path=%s", repo_path)

        # 1) scan
        t0 = time.perf_counter()
        try:
            files = scan_repo(repo_path)
        except Exception:
            log.exception("ingestion_pipeline: scan_repo failed")
            raise
        scan_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "ingestion_pipeline: scanned files=%d scan_ms=%.1f", len(files), scan_ms
        )

        # 2) chunk (continue on per-file errors)
        t0 = time.perf_counter()
        all_chunks: list[dict[str, Any]] = []
        files_processed = 0
        file_errors = 0
        for fd in files:
            try:
                chunks = chunk_code(fd, org_id=org_id, repo_id=repo_id)
                files_processed += 1
                all_chunks.extend(chunks)
            except Exception as exc:  # noqa: BLE001
                file_errors += 1
                log.warning(
                    "ingestion_pipeline: chunk_code failed file=%s: %s",
                    fd.get("file_path") or fd.get("file"),
                    exc,
                )
                continue
            if len(all_chunks) >= _INGEST_MAX_CHUNKS:
                log.warning(
                    "ingestion_pipeline: chunk cap %d reached; ignoring remaining files",
                    _INGEST_MAX_CHUNKS,
                )
                break
        chunk_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "ingestion_pipeline: chunked files=%s chunks=%d errors=%d chunk_ms=%.1f",
            files_processed,
            len(all_chunks),
            file_errors,
            chunk_ms,
        )

        if not all_chunks:
            _mark_indexed_metadata(repo_path, 0)
            log.info(
                "ingestion_pipeline: no chunks, repo marked INDEXED (empty). "
                "files_processed=%d", files_processed
            )
            return {
                "files_processed": files_processed,
                "chunks_created": 0,
                "chunks_inserted": 0,
            }

        # 3) embed in batches
        embeddings: list[list[float]] = []
        t0 = time.perf_counter()
        for batch_start in range(0, len(all_chunks), _EMBED_BATCH_SIZE):
            batch = all_chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
            texts = [_truncate_for_embedding(c.get("code", "")) for c in batch]
            try:
                vecs = generate_embeddings_batch(texts, model_id=embedding_model_id)
            except Exception:
                log.exception(
                    "ingestion_pipeline: embedding batch failed offset=%d size=%d",
                    batch_start,
                    len(batch),
                )
                raise
            if len(vecs) != len(batch):
                raise RuntimeError(
                    f"embedding count mismatch (got {len(vecs)} for {len(batch)} chunks)"
                )
            embeddings.extend(vecs)
        embed_ms = (time.perf_counter() - t0) * 1000.0
        dim = len(embeddings[0]) if embeddings else None
        log.info(
            "ingestion_pipeline: embedded chunks=%d dim=%s embed_ms=%.1f",
            len(embeddings),
            dim,
            embed_ms,
        )

        # 4) load/init index, then insert
        _safe_init_or_load(org_id, dimension=dim)
        rows: list[dict[str, Any]] = []
        for chunk, vec in zip(all_chunks, embeddings):
            rows.append(
                {
                    "chunk_hash": chunk.get("chunk_hash", ""),
                    "embedding": vec,
                    # Keep the whole chunk accessible to retrieval_service (uses
                    # metadata.code / .file / .symbol / .repo_id / .org_id)
                    "metadata": {
                        "org_id": org_id,
                        "repo_id": repo_id,
                        "file": chunk.get("file", ""),
                        "symbol": chunk.get("symbol", ""),
                        "language": chunk.get("language", ""),
                        "start_line": chunk.get("start_line", 0),
                        "end_line": chunk.get("end_line", 0),
                        "code": chunk.get("code", ""),
                    },
                }
            )

        t0 = time.perf_counter()
        result = index_chunks(org_id, rows)
        save_index(org_id)  # explicit per requirements (index_chunks also saves)
        insert_ms = (time.perf_counter() - t0) * 1000.0
        added = int(result.get("added", 0))
        skipped = int(result.get("skipped", 0))

        # 5) repo metadata
        _mark_indexed_metadata(repo_path, added)

        # 6) refresh org knowledge map from the full vector store (best-effort;
        #    covers every repo in the org, not just this one)
        try:
            state = load_index(org_id)
            generate_org_map(org_id, list(state.entries))
        except Exception:  # noqa: BLE001
            log.exception(
                "ingestion_pipeline: generate_org_map failed (continuing)"
            )

        total_ms = (time.perf_counter() - t_all) * 1000.0
        log.info(
            "ingestion_pipeline: done files_processed=%d chunks_created=%d "
            "chunks_inserted=%d chunks_skipped=%d insert_ms=%.1f total_ms=%.1f",
            files_processed,
            len(all_chunks),
            added,
            skipped,
            insert_ms,
            total_ms,
        )

        return {
            "files_processed": files_processed,
            "chunks_created": len(all_chunks),
            "chunks_inserted": added,
        }


def _mark_indexed_metadata(repo_path: str, added: int) -> None:
    """Best-effort filesystem metadata update (doesn't fail the ingest run)."""
    try:
        merge_metadata(
            repo_path,
            {
                "status": "INDEXED",
                "last_indexed": _iso_utc_now(),
                "last_indexed_chunks": added,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "ingestion_pipeline: failed to update repo metadata (continuing)"
        )


__all__ = ["ingest_repo"]
