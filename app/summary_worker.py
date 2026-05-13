"""Post-ingest code-understanding worker.

Given an ``org_id``, walk every chunk in that org's vector store that does not
yet have a ``summary`` field, call :func:`app.code_understanding.summarize_chunk`
for each, and persist the structured result back into the chunk's metadata via
:func:`app.vector_store.update_chunk_metadata`.

Design notes
------------
* **Never touches embeddings.** The only write path is
  ``update_chunk_metadata`` which rewrites the JSON sidecar while leaving the
  FAISS index bytes unchanged.
* **Retry policy.** Each chunk gets up to :data:`_MAX_CHUNK_ATTEMPTS` attempts
  (default 2). A failed chunk is re-queued at the back of the work list and
  retried in a later batch. After the cap is hit, the chunk is recorded as
  ``failed`` and the worker moves on — so a single hostile chunk can never
  block the rest of the queue. The upper bound on LLM calls is therefore
  ``O(_MAX_CHUNK_ATTEMPTS × pending)``.
* **Bounded per-run work.** Respects ``summarization.max_chunks_per_run``.
  Anything beyond the cap is counted as ``deferred`` and left for the next
  invocation.
* **Safe to run unconditionally.** When ``summarization.enabled`` is ``false``
  the function returns a fully-populated stats dict with ``enabled=False``
  without calling any other services — so a cron / event-driven caller does
  not need to gate on the flag itself.

The function returns a stats dict so callers (tests, CLI scripts, event
handlers) can assert on the results:

    {
        "enabled":    bool,
        "fetched":    int,   # unsummarized chunks found in the store
        "processed":  int,   # attempted in this run (<= max_chunks_per_run)
        "succeeded":  int,   # updated metadata successfully
        "failed":     int,   # exhausted retries, skipped
        "deferred":   int,   # left over because of max_chunks_per_run
        "retries":    int,   # retry events issued (telemetry only)
        "elapsed_ms": float,
    }
"""

from __future__ import annotations

import time
from typing import Any, Final, Iterator

from app.code_understanding import summarize_chunk
from app.config import get_settings
from app.logging import get_logger, log_context
from app.vector_store import get_chunks_without_summary, update_chunk_metadata

log = get_logger(__name__)

# Total attempts per chunk before giving up: 1 initial + (N-1) retries.
# 2 is enough to absorb transient Bedrock throttles without letting a
# permanently-broken chunk (e.g. malformed JSON no matter what) waste budget.
_MAX_CHUNK_ATTEMPTS: Final[int] = 2


def _iter_batches(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Yield successive slices of length ``size`` from ``items``."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _empty_stats(enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "fetched": 0,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "deferred": 0,
        "retries": 0,
        "elapsed_ms": 0.0,
    }


def process_unsummarized_chunks(org_id: str) -> dict[str, Any]:
    """Summarise every still-unsummarized chunk in ``org_id``'s vector store.

    Honours ``settings.summarization.enabled``,
    ``settings.summarization.batch_size``, and
    ``settings.summarization.max_chunks_per_run`` (see
    :class:`app.config.SummarizationSettings`).

    Returns a stats dict; never raises for per-chunk errors (those are
    retried / skipped and counted in ``failed``). Only raises for
    configuration-level problems (empty ``org_id``).
    """
    if not isinstance(org_id, str) or not org_id.strip():
        raise ValueError("org_id must be a non-empty string")
    org_id = org_id.strip()

    cfg = get_settings().summarization
    if not cfg.enabled:
        log.info(
            "summary_worker: skipped org_id=%s reason=disabled batch_size=%s max_per_run=%s",
            org_id,
            cfg.batch_size,
            cfg.max_chunks_per_run,
        )
        return _empty_stats(enabled=False)

    batch_size = int(cfg.batch_size)
    max_chunks_per_run = int(cfg.max_chunks_per_run)

    with log_context(org_id=org_id):
        t0 = time.perf_counter()

        all_pending = get_chunks_without_summary(org_id)
        fetched = len(all_pending)
        if fetched == 0:
            stats = _empty_stats(enabled=True)
            stats["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            log.info(
                "summary_worker: nothing to do org_id=%s fetched=0 elapsed_ms=%.1f",
                org_id,
                stats["elapsed_ms"],
            )
            return stats

        if fetched > max_chunks_per_run:
            pending = all_pending[:max_chunks_per_run]
            deferred = fetched - max_chunks_per_run
            log.info(
                "summary_worker: queue capped org_id=%s fetched=%s limit=%s deferred=%s",
                org_id,
                fetched,
                max_chunks_per_run,
                deferred,
            )
        else:
            pending = all_pending
            deferred = 0

        # Work queue preserves arrival order; retried chunks are appended to
        # the back so they don't starve fresh ones.
        queue: list[dict[str, Any]] = list(pending)
        attempts: dict[str, int] = {}
        succeeded = 0
        failed: list[str] = []
        retries = 0
        batch_number = 0

        log.info(
            "summary_worker: start org_id=%s processed=%s batch_size=%s max_attempts=%s",
            org_id,
            len(pending),
            batch_size,
            _MAX_CHUNK_ATTEMPTS,
        )

        while queue:
            batch_number += 1
            batch = queue[:batch_size]
            queue = queue[batch_size:]

            b_success = 0
            b_retry = 0
            b_fail = 0

            for item in batch:
                chunk_hash = str(item.get("chunk_hash") or "").strip()
                if not chunk_hash:
                    # Defensive: vector_store returns a non-empty string, but
                    # still guard against corrupt metadata.
                    b_fail += 1
                    failed.append("(missing chunk_hash)")
                    log.error(
                        "summary_worker: skipping row with empty chunk_hash org_id=%s",
                        org_id,
                    )
                    continue

                attempts[chunk_hash] = attempts.get(chunk_hash, 0) + 1
                attempt_no = attempts[chunk_hash]

                meta = item.get("metadata") or {}
                if not isinstance(meta, dict):
                    meta = {}
                llm_input = {**meta, "chunk_hash": chunk_hash}

                # --- 1) summarise ---
                try:
                    result = summarize_chunk(llm_input)
                except Exception as exc:  # noqa: BLE001 — we deliberately catch
                    if attempt_no < _MAX_CHUNK_ATTEMPTS:
                        queue.append(item)
                        retries += 1
                        b_retry += 1
                        log.warning(
                            "summary_worker: summarize failed, will retry "
                            "attempt=%s/%s org_id=%s chunk_hash=%s error=%s",
                            attempt_no,
                            _MAX_CHUNK_ATTEMPTS,
                            org_id,
                            chunk_hash,
                            exc,
                        )
                    else:
                        failed.append(chunk_hash)
                        b_fail += 1
                        log.error(
                            "summary_worker: summarize failed, giving up "
                            "attempts=%s org_id=%s chunk_hash=%s error=%s",
                            attempt_no,
                            org_id,
                            chunk_hash,
                            exc,
                        )
                    continue

                # --- 2) persist ---
                try:
                    update_chunk_metadata(org_id, chunk_hash, result)
                except Exception as exc:  # noqa: BLE001
                    # Persistence errors are treated the same as LLM errors:
                    # they are almost always transient (lock timeout, FS
                    # hiccup) and worth one more try.
                    if attempt_no < _MAX_CHUNK_ATTEMPTS:
                        queue.append(item)
                        retries += 1
                        b_retry += 1
                        log.warning(
                            "summary_worker: metadata update failed, will retry "
                            "attempt=%s/%s org_id=%s chunk_hash=%s error=%s",
                            attempt_no,
                            _MAX_CHUNK_ATTEMPTS,
                            org_id,
                            chunk_hash,
                            exc,
                        )
                    else:
                        failed.append(chunk_hash)
                        b_fail += 1
                        log.error(
                            "summary_worker: metadata update failed, giving up "
                            "attempts=%s org_id=%s chunk_hash=%s error=%s",
                            attempt_no,
                            org_id,
                            chunk_hash,
                            exc,
                        )
                    continue

                succeeded += 1
                b_success += 1

            log.info(
                "summary_worker: batch=%s done org_id=%s size=%s success=%s retry=%s "
                "fail=%s queue_remaining=%s",
                batch_number,
                org_id,
                len(batch),
                b_success,
                b_retry,
                b_fail,
                len(queue),
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        processed = len(pending)
        stats: dict[str, Any] = {
            "enabled": True,
            "fetched": fetched,
            "processed": processed,
            "succeeded": succeeded,
            "failed": len(failed),
            "deferred": deferred,
            "retries": retries,
            "elapsed_ms": elapsed_ms,
        }

        log.info(
            "summary_worker: done org_id=%s fetched=%s processed=%s succeeded=%s "
            "failed=%s deferred=%s retries=%s elapsed_ms=%.1f",
            org_id,
            fetched,
            processed,
            succeeded,
            len(failed),
            deferred,
            retries,
            elapsed_ms,
        )
        return stats


__all__ = ["process_unsummarized_chunks"]
