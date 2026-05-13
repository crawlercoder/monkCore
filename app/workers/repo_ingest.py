"""Background worker that ingests a repo: fetch → clone → index → mark READY.

Flow
----
1. Load the ``Repo`` row from DynamoDB.
2. Atomically transition ``PENDING → CLONING`` via a conditional update;
   if the transition fails because the row is already ``CLONING`` or
   ``READY`` we bail with a typed skip so the caller can react.
3. Fetch the owning org's GitLab token from Secrets Manager.
4. Clone (or idempotently refresh) the repo into
   ``<BASE_STORAGE_PATH>/repos/<org_id>/<repo_id>`` (see :mod:`app.storage_manager`).
5. **Index** the repo with :func:`app.ingestion_pipeline.ingest_repo`
   (batched embeddings into the org's FAISS store, then refresh the
   org knowledge map). Repo filesystem metadata transitions
   ``CLONED → INDEXING → READY``.
6. Transition ``CLONING → READY`` on success.
7. **Schedule LLM summarisation (fire-and-forget).** As soon as the
   repo is READY we kick off
   :func:`app.summary_worker.process_unsummarized_chunks` in a
   worker thread so the request/worker does not block on LLM latency.
   The summariser only reads chunks whose ``metadata.summary`` is
   missing, so it is intrinsically retry-safe and will never
   reprocess work that has already been done.
8. On any error between steps 3 and 6, best-effort revert to ``PENDING``
   so a retry can pick the row back up, and re-raise the original
   exception for the caller (SQS/EventBridge/ECS task runner) to
   classify.

Public API
----------
* :func:`process_repo` — module-level coroutine matching the requested
  ``(repo_id)`` signature.
* :class:`RepoIngestWorker` — class form for dependency injection.

Status-machine invariants
-------------------------
* Only one worker at a time can hold ``CLONING`` for a given ``repo_id``
  (enforced by a conditional write in DynamoDB, not an in-process lock,
  so it's safe across Lambda invocations / ECS tasks / laptop runs).
* Destination directory writes are additionally guarded by a
  ``FileLock`` inside ``git_clone``; safe even if the status invariant
  ever slips due to operator intervention.
* On failure we revert to ``PENDING`` rather than invent a ``FAILED``
  state — the repo model intentionally has only three states. Callers
  that need "don't retry" semantics should catch the typed exception
  (``GitAuthError``, ``GitRepoNotFoundError``, etc.) and suppress
  re-enqueue themselves.
"""

from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app import storage_manager
from app.config import Settings, get_settings
from app.db.repos import (
    RepoNotFoundError,
    RepoStatusConflictError,
    ReposRepository,
)
from app.ingestion_pipeline import ingest_repo as default_ingest_repo
from app.knowledge_map import generate_org_map
from app.logging import (
    get_logger,
    log_context,
    log_event,
    log_status_transition,
)
from app.models.repos import Repo, RepoStatus, RepoUpdate
from app.repo_initializer import merge_metadata
from app.services.git_clone import (
    GitCloneService,
    clone_repo as default_clone_repo,
)
from app.services.gitlab_tokens import GitlabTokensService
from app.summary_worker import process_unsummarized_chunks as default_summarize_org
from app.vector_store import load_index

log = get_logger(__name__)

# Anchors for fire-and-forget background ``asyncio.Task``s (e.g. the
# post-ingest LLM summarisation pass). ``asyncio.create_task`` only keeps a
# weak reference on the loop, so without an explicit strong reference the
# task can be garbage-collected mid-run and cancelled — dropping summaries
# silently. Tasks remove themselves from the set via ``add_done_callback``.
_BG_TASKS: "set[asyncio.Task[object]]" = set()

# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #

class RepoIngestError(Exception):
    """Base class for repo-ingest worker errors that aren't re-raised from
    lower layers. Lower-layer exceptions (``GitCloneError`` subclasses,
    ``RepoNotFoundError``, ``OrgNotFoundError``) are propagated unchanged
    so the caller can key its retry policy on the specific type."""


class RepoIngestSkipped(RepoIngestError):
    """Work was not performed because the row is already terminal or in-flight.

    Not a failure: callers typically log at INFO and move on.
    """

    def __init__(self, repo_id: str, reason: str, current_status: Optional[RepoStatus] = None):
        super().__init__(f"repo '{repo_id}' skipped: {reason}")
        self.repo_id = repo_id
        self.reason = reason
        self.current_status = current_status


# --------------------------------------------------------------------------- #
# Worker                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class RepoIngestWorker:
    """Process a single repo end-to-end.

    Dependencies are constructor-injected so tests can replace the DB
    layer with an in-memory fake, the tokens service with a mock, and
    ``clone_fn`` with a coroutine that just returns a ``Path``.
    """

    repos_repo: Optional[ReposRepository] = None
    tokens_service: Optional[GitlabTokensService] = None
    clone_service: Optional[GitCloneService] = None
    settings: Optional[Settings] = None
    # Synchronous ingest callable; defaults to :func:`app.ingestion_pipeline.ingest_repo`.
    # Injected via a field so tests can pass a fast no-op.
    ingest_fn: Optional[Callable[[str, str, str], dict[str, int]]] = None
    # Synchronous summariser callable; defaults to
    # :func:`app.summary_worker.process_unsummarized_chunks`. Runs in a
    # worker thread via ``asyncio.to_thread`` so we never block the event
    # loop on Bedrock.
    summarize_fn: Optional[Callable[[str], dict[str, object]]] = None
    # Number of application-level retries for the indexing stage on transient errors.
    ingest_max_attempts: int = 3
    ingest_backoff_base_s: float = 2.0
    ingest_backoff_max_s: float = 30.0

    def __post_init__(self) -> None:
        self.settings = self.settings or get_settings()
        self.repos_repo = self.repos_repo or ReposRepository(self.settings)
        self.tokens_service = self.tokens_service or GitlabTokensService()
        self.ingest_fn = self.ingest_fn or default_ingest_repo
        self.summarize_fn = self.summarize_fn or default_summarize_org
        # clone_service may stay None; we'll fall back to the module-level
        # clone_repo() to preserve its own defaults.

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #

    async def process_repo(self, repo_id: str) -> Repo:
        """Ingest ``repo_id``. Returns the row in its final ``READY`` state.

        * Raises :class:`RepoNotFoundError` if the repo row is missing.
        * Raises :class:`RepoIngestSkipped` if the row is already
          ``READY`` or another worker already owns ``CLONING``.
        * Re-raises :class:`GitCloneError` subclasses unchanged on clone
          failure (after best-effort reverting status to ``PENDING``).

        All downstream logs (DB ops, git subprocess, secrets fetch) are
        bound to ``repo_id`` / ``org_id`` via :func:`log_context`, so a
        single ``jq 'select(.repo_id == "...")'`` filter traces the job.
        """
        if not repo_id:
            raise ValueError("repo_id must be a non-empty string")

        # Bind repo_id as early as possible so even the "row not found"
        # log line carries it.
        with log_context(repo_id=repo_id):
            repo = await self.repos_repo.get(repo_id)
            if repo is None:
                log_event(log, "repo_ingest.not_found",
                          "repo row missing", level=logging.WARNING)
                raise RepoNotFoundError(f"repo '{repo_id}' not found")

            # Now bind org_id + branch for the rest of the pipeline.
            with log_context(org_id=repo.org_id, branch=repo.branch):
                return await self._process_loaded(repo)

    async def _process_loaded(self, repo: Repo) -> Repo:
        """Run the claim → clone → READY pipeline for a loaded row.

        Split out so :meth:`process_repo` only has to deal with the
        early-exit cases (missing row, already READY) and leaves the
        long-running work to a method that assumes ``repo`` is real.
        """
        repo_id = repo.repo_id
        started = time.perf_counter()

        log_event(log, "repo_ingest.started", "repo ingestion started",
                  current_status=repo.status.value)

        if repo.status is RepoStatus.READY:
            log_event(log, "repo_ingest.skipped",
                      "already READY; nothing to do",
                      reason="already_ready")
            raise RepoIngestSkipped(
                repo_id, "already READY", current_status=RepoStatus.READY
            )

        # ---- claim: PENDING -> CLONING -------------------------------- #
        try:
            repo = await self.repos_repo.compare_and_set_status(
                repo_id, expected=RepoStatus.PENDING, new=RepoStatus.CLONING,
            )
        except RepoStatusConflictError as exc:
            log_event(log, "repo_ingest.claim_failed",
                      f"could not claim row: {exc}",
                      reason="status_conflict")
            raise RepoIngestSkipped(
                repo_id, f"claim failed: {exc}", current_status=None,
            ) from exc

        log_status_transition(
            log, entity="repo",
            from_status=RepoStatus.PENDING.value,
            to_status=RepoStatus.CLONING.value,
            trigger="repo_ingest.claim",
        )

        # ---- fetch token, clone, index, mark READY -------------------- #
        try:
            token = await self.tokens_service.get_gitlab_token_for_org(repo.org_id)
            dest = self._destination_for(repo)

            with log_context(dest=str(dest)):
                # ---- stage 1: clone (idempotent) ---------------------- #
                log_event(log, "repo_ingest.clone_start",
                          "starting clone", repo_url=_safe_url(repo.repo_url))
                clone_t0 = time.perf_counter()
                if self.clone_service is not None:
                    await self.clone_service.clone_repo(
                        repo.repo_url, token, dest, branch=repo.branch,
                    )
                else:
                    await default_clone_repo(
                        repo.repo_url, token, dest, branch=repo.branch,
                    )
                clone_ms = round((time.perf_counter() - clone_t0) * 1000, 2)
                log_event(log, "repo_ingest.clone_completed",
                          "clone complete", duration_ms=clone_ms)

                # The cloner writes ``.agent/metadata.json`` with
                # status="CLONED"; keep that as our filesystem start-of-
                # indexing marker (it's the "CLONED" in CLONED → INDEXING).
                self._fs_status(dest, "INDEXING")
                log_status_transition(
                    log, entity="repo_fs",
                    from_status="CLONED",
                    to_status="INDEXING",
                    trigger="repo_ingest.indexing_start",
                )

                # ---- stage 2: ingest (batched, idempotent, retried) --- #
                index_t0 = time.perf_counter()
                stats = await self._run_ingestion_with_retries(
                    org_id=repo.org_id, repo_id=repo_id, dest=dest,
                )
                index_ms = round((time.perf_counter() - index_t0) * 1000, 2)
                log_event(
                    log, "repo_ingest.indexing_completed",
                    "indexing complete",
                    duration_ms=index_ms,
                    files_processed=int(stats.get("files_processed", 0)),
                    chunks_created=int(stats.get("chunks_created", 0)),
                    chunks_inserted=int(stats.get("chunks_inserted", 0)),
                )

                # ---- stage 3: refresh org-level knowledge map --------- #
                try:
                    await asyncio.to_thread(_regenerate_org_map, repo.org_id)
                    log_event(
                        log, "repo_ingest.knowledge_map_regenerated",
                        "org knowledge map regenerated",
                    )
                except Exception as map_exc:  # noqa: BLE001
                    # Non-fatal: map is a derived artifact. Missing it
                    # shouldn't pin the repo to CLONING forever.
                    log_event(
                        log, "repo_ingest.knowledge_map_failed",
                        f"knowledge map refresh failed: {map_exc}",
                        level=logging.WARNING,
                        exc_info=True,
                    )

                self._fs_status(dest, "READY")
                log_status_transition(
                    log, entity="repo_fs",
                    from_status="INDEXING",
                    to_status="READY",
                    trigger="repo_ingest.indexing_done",
                )

            # ---- stage 4: DynamoDB CLONING → READY -------------------- #
            final = await self.repos_repo.update(
                repo_id, RepoUpdate(status=RepoStatus.READY),
            )
            log_status_transition(
                log, entity="repo",
                from_status=RepoStatus.CLONING.value,
                to_status=RepoStatus.READY.value,
                trigger="repo_ingest.finish",
            )

            # ---- stage 5: LLM summarisation (async, fire-and-forget) - #
            # The repo is officially READY. The summariser is retry-safe
            # (skips chunks that already have ``metadata.summary``) and
            # runs in a thread so event-loop latency and Bedrock latency
            # are fully decoupled. Failures here never fail the ingest.
            self._schedule_summarization(repo.org_id)

            log_event(log, "repo_ingest.finished",
                      "repo is READY",
                      duration_ms=round((time.perf_counter() - started) * 1000, 2))
            return final

        except Exception as exc:
            log_event(
                log, "repo_ingest.failed",
                f"ingestion failed: {exc}",
                level=logging.ERROR,
                exc_info=True,
                error_type=exc.__class__.__name__,
            )
            await self._revert_to_pending(repo_id)
            raise

    async def _run_ingestion_with_retries(
        self, *, org_id: str, repo_id: str, dest: Path,
    ) -> dict[str, int]:
        """Run :func:`ingest_repo` in a worker thread with bounded retries.

        Idempotent because :mod:`app.vector_store` de-duplicates by
        ``chunk_hash``; retries will not double-insert rows.
        """
        assert self.ingest_fn is not None
        attempts = max(1, int(self.ingest_max_attempts))
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                stats: dict[str, int] = await asyncio.to_thread(
                    self.ingest_fn, org_id, repo_id, str(dest),
                )
                if attempt > 1:
                    log_event(log, "repo_ingest.indexing_retry_ok",
                              "indexing succeeded after retry",
                              attempt=attempt)
                return stats
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= attempts:
                    break
                sleep_s = min(
                    self.ingest_backoff_base_s ** (attempt - 1)
                    + random.random() * 0.5,
                    self.ingest_backoff_max_s,
                )
                log_event(
                    log, "repo_ingest.indexing_retry",
                    f"indexing failed (attempt {attempt}/{attempts}), retrying "
                    f"in {sleep_s:.1f}s: {exc}",
                    level=logging.WARNING,
                    error_type=exc.__class__.__name__,
                    attempt=attempt,
                    max_attempts=attempts,
                )
                await asyncio.sleep(sleep_s)
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ #
    # Fire-and-forget LLM summarisation                                  #
    # ------------------------------------------------------------------ #

    def _schedule_summarization(self, org_id: str) -> None:
        """Kick off the summariser for ``org_id`` without awaiting it.

        Design goals, per requirements:

        * Runs **independently** of the ingest pipeline — not awaited,
          not joined to the current task, not part of the READY
          transition. The repo is already READY before we get here.
        * **Does not block ingestion flow** — the sync worker is
          dispatched via ``asyncio.to_thread`` and wrapped in an
          ``asyncio.Task`` the ingest handler does not ``await``.
        * **Safe to retry** — the underlying
          :func:`app.summary_worker.process_unsummarized_chunks`
          reads only chunks whose ``metadata.summary`` is missing, so
          a second (or third, or Nth) trigger against the same org is
          an idempotent no-op for already-summarised chunks.
        * **Disabled path is cheap** — we short-circuit before creating
          any task when ``summarization.enabled`` is False to keep logs
          clean on orgs that opt out.
        """
        if not (org_id or "").strip():
            return

        cfg = getattr(self.settings, "summarization", None) if self.settings else None
        if cfg is not None and not bool(getattr(cfg, "enabled", True)):
            log_event(
                log, "repo_ingest.summary_skipped",
                "summary_worker disabled by settings; not scheduling",
                org_id=org_id,
                reason="disabled",
            )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — e.g. a unit test that calls the worker
            # synchronously via ``asyncio.run(...)``. The caller has
            # already returned, so there's no loop to create a task on.
            log_event(
                log, "repo_ingest.summary_not_scheduled",
                "no running event loop; skipping fire-and-forget summary",
                org_id=org_id,
                level=logging.WARNING,
            )
            return

        task = loop.create_task(
            self._run_summarization(org_id),
            name=f"summary-worker:{org_id}",
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        log_event(
            log, "repo_ingest.summary_scheduled",
            "scheduled background summarization",
            org_id=org_id,
            task_name=task.get_name(),
        )

    async def _run_summarization(self, org_id: str) -> None:
        """Coroutine body for the background summary task.

        Catches *everything* so no unhandled exception escapes into the
        event loop — the repo is already READY and the caller does not
        (cannot) await us. Any failure is logged and swallowed; the
        next ingest for this org will re-trigger summarisation and
        pick up anything still pending.
        """
        assert self.summarize_fn is not None
        with log_context(org_id=org_id):
            t0 = time.perf_counter()
            log_event(
                log, "repo_ingest.summary_started",
                "background summarization started",
            )
            try:
                stats = await asyncio.to_thread(self.summarize_fn, org_id)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    log, "repo_ingest.summary_failed",
                    f"background summarization raised: {exc}",
                    level=logging.ERROR,
                    exc_info=True,
                    duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                    error_type=exc.__class__.__name__,
                )
                return

            # ``stats`` is whatever the injected summariser returned; we
            # only flatten known numeric fields into the log record so
            # an alternate summariser can't break structured logging.
            safe_stats: dict[str, object] = {}
            if isinstance(stats, dict):
                for key in ("enabled", "fetched", "processed", "succeeded",
                            "failed", "deferred", "retries", "elapsed_ms"):
                    if key in stats:
                        safe_stats[key] = stats[key]
            log_event(
                log, "repo_ingest.summary_completed",
                "background summarization finished",
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                **safe_stats,
            )

    def _fs_status(self, dest: Path, status: str) -> None:
        """Update filesystem ``.agent/metadata.json`` status best-effort."""
        try:
            merge_metadata(str(dest), {"status": status})
        except Exception:  # noqa: BLE001
            log_event(
                log, "repo_ingest.fs_status_update_failed",
                f"failed to write fs status {status!r} under {dest}",
                level=logging.WARNING,
                exc_info=True,
            )

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _destination_for(self, repo: Repo) -> Path:
        """Derive a stable, validated clone path for a repo.

        Same layout as :func:`app.storage_manager.get_repo_path` — under
        :attr:`app.config.Settings.base_storage_path` (``BASE_STORAGE_PATH``,
        e.g. ``/ai-agent/repos/...``), not :attr:`Settings.workspace_root``.
        """
        raw = storage_manager.get_repo_path(repo.org_id, repo.repo_id)
        return Path(raw.rstrip("/\\")).resolve()

    async def _revert_to_pending(self, repo_id: str) -> None:
        """Best-effort status rollback after a failure.

        Uses the unguarded ``update`` (not compare-and-set) on purpose:
        we want to force PENDING regardless of the current value, because
        the only legitimate state at this point is CLONING (we claimed
        it) or something surprising (operator intervention), and both
        should flip to PENDING so retries can proceed.
        """
        try:
            await self.repos_repo.update(repo_id, RepoUpdate(status=RepoStatus.PENDING))
            log_status_transition(
                log, entity="repo",
                from_status=RepoStatus.CLONING.value,
                to_status=RepoStatus.PENDING.value,
                trigger="repo_ingest.revert",
            )
        except Exception:
            # Nothing to do but shout: the row is stuck in CLONING and a
            # human (or a reaper job) will need to reset it.
            log_event(
                log, "repo_ingest.revert_failed",
                "FAILED to revert status to PENDING",
                level=logging.ERROR,
                exc_info=True,
            )

    # ------------------------------------------------------------------ #
    # Utilities (tests / operators)                                      #
    # ------------------------------------------------------------------ #

    async def purge_workspace(self, repo: Repo) -> None:
        """Remove the local checkout for ``repo``. Useful for forced re-ingest."""
        dest = self._destination_for(repo)
        if dest.exists():
            with log_context(repo_id=repo.repo_id, org_id=repo.org_id):
                log_event(log, "repo_ingest.purge_workspace",
                          "removing local checkout", dest=str(dest))
            shutil.rmtree(dest, ignore_errors=False)


def _regenerate_org_map(org_id: str) -> None:
    """Rebuild the org-wide knowledge map from the full FAISS store.

    Called from the worker after a successful ingest. Uses the vector
    store's in-memory ``entries`` (already keyed per chunk metadata) so
    the map reflects *every* repo in the org, not just the one that was
    just ingested.
    """
    state = load_index(org_id)
    generate_org_map(org_id, list(state.entries))


def _safe_url(repo_url: str) -> str:
    """Drop any userinfo from ``repo_url`` before it hits a log line.

    Defense-in-depth: ``repo_url`` from the DB should never carry a token,
    but if an operator uploaded one by mistake we still want redacted
    logs. Kept local to avoid a circular import with ``git_clone``.
    """
    from urllib.parse import urlparse, urlunparse

    try:
        p = urlparse(repo_url)
    except Exception:
        return repo_url
    if not (p.username or p.password):
        return repo_url
    host = p.hostname or ""
    netloc = f"{host}:{p.port}" if p.port else host
    return urlunparse(p._replace(netloc=netloc))


# --------------------------------------------------------------------------- #
# Module-level convenience                                                    #
# --------------------------------------------------------------------------- #

_default_worker: Optional[RepoIngestWorker] = None


def _worker() -> RepoIngestWorker:
    global _default_worker
    if _default_worker is None:
        _default_worker = RepoIngestWorker()
    return _default_worker


async def process_repo(repo_id: str) -> Repo:
    """Ingest ``repo_id`` end-to-end using the default worker singleton.

    Thin wrapper so schedulers (Lambda handlers, CLI, cron, tests) can
    import a single function.

    Example::

        from app.workers.repo_ingest import process_repo
        repo = await process_repo("repo_abc123...")
    """
    return await _worker().process_repo(repo_id)


__all__ = [
    "RepoIngestError",
    "RepoIngestSkipped",
    "RepoIngestWorker",
    "process_repo",
]
