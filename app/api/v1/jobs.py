"""Job endpoints.

* ``POST /v1/jobs``           — create an AI-agent job and fire the
  :mod:`app.mr_pipeline` background worker. Returns the generated
  ``job_id`` and a ``status`` of ``"CREATED"``.
* ``GET  /v1/jobs/{job_id}``  — fetch a job's current state: uppercase
  ``status``, ``mr_url``, ``staging_url``, and a tail of the
  per-job log file.

The HTTP layer is intentionally thin: the actual work lives in
:func:`app.mr_pipeline.run_job`. Writes and reads of the DynamoDB row
go through :class:`app.db.dynamodb.JobsRepository`; artifact reads
(summary + logs) go through :mod:`app.artifact_manager`.

Background execution
--------------------
We schedule the pipeline with :func:`asyncio.create_task` rather than
FastAPI's :class:`BackgroundTasks` so the POST response returns
immediately — ``BackgroundTasks`` would run the coroutine *after* the
response object is sent but still block the request handler from
finishing in some ASGI server configurations. A module-level set
(:data:`_background_tasks`) keeps references alive so the runtime
doesn't garbage-collect tasks that no-one is awaiting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_registry_service
from app.artifact_manager import init_job_artifacts, read_logs, read_summary
from app.db.dynamodb import DynamoDBError, JobsRepository
from app.db.orgs import OrgNotFoundError
from app.errors import BadRequestError, ConflictError, NotFoundError, UpstreamError
from app.logging import get_logger, log_context, log_event
from app.models.jobs import Job, JobId, JobStatus, SpecText, api_status
from app.models.orgs import OrgId
from app.mr_pipeline import run_job
from app.services.registry import RegistryService

log = get_logger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas                                                                     #
# --------------------------------------------------------------------------- #


class JobCreateRequest(BaseModel):
    """Payload for ``POST /v1/jobs``."""

    model_config = ConfigDict(extra="forbid")

    org_id: OrgId = Field(..., description="Owning organization id")
    spec: SpecText = Field(
        ...,
        description=(
            "Free-form product / change spec. The agent converts this into"
            " a concrete multi-repo plan."
        ),
    )


class JobCreateResponse(BaseModel):
    """Response for ``POST /v1/jobs``.

    ``status`` is always :attr:`JobStatus.CREATED` on success — the
    actual pipeline runs asynchronously; poll
    ``GET /v1/jobs/{job_id}`` for progress.
    """

    model_config = ConfigDict(use_enum_values=True)

    job_id: JobId
    status: JobStatus = Field(default=JobStatus.CREATED)


# --------------------------------------------------------------------------- #
# Background worker plumbing                                                  #
# --------------------------------------------------------------------------- #


_background_tasks: set[asyncio.Task[Any]] = set()
# job_id -> running pipeline task; same process only (multi-worker cancel is best-effort).
_pipeline_tasks: dict[str, asyncio.Task[Any]] = {}

# Stored Dynamo status values (lowercase) that must not accept cancel.
_TERMINAL_DB_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


def _schedule_pipeline(job_id: str) -> None:
    """Fire ``run_job(job_id)`` into the current event loop, fire-and-forget.

    Keeps a strong reference to the task so the loop doesn't drop it
    and logs any unhandled exception on completion — the pipeline
    itself already flips the DB row to ``failed`` before re-raising, so
    the callback's job is just observability, not recovery.
    """
    task = asyncio.create_task(run_job(job_id), name=f"mr_pipeline:{job_id}")
    _background_tasks.add(task)
    _pipeline_tasks[job_id] = task

    def _done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        _pipeline_tasks.pop(job_id, None)
        if t.cancelled():
            log_event(
                log, "api.jobs.pipeline_cancelled",
                "mr_pipeline task was cancelled", job_id=job_id,
                level=logging.WARNING,
            )
            return
        exc = t.exception()
        if exc is not None:
            log_event(
                log, "api.jobs.pipeline_error",
                "mr_pipeline task raised", job_id=job_id,
                level=logging.ERROR, exc_info=exc,
                error_type=exc.__class__.__name__,
            )

    task.add_done_callback(_done)


# --------------------------------------------------------------------------- #
# POST /v1/jobs                                                               #
# --------------------------------------------------------------------------- #


@router.post(
    "/jobs",
    response_model=JobCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent job and trigger the MR pipeline",
    responses={
        201: {"description": "Job created and pipeline scheduled"},
        400: {"description": "Invalid input"},
        404: {"description": "org_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def create_job(payload: JobCreateRequest) -> JobCreateResponse:
    """Persist a new job row and kick off the end-to-end pipeline.

    * Validates that ``org_id`` exists so the UI gets a clean 404 up
      front rather than a delayed "failed" state from the worker.
    * Writes ``{job_id, org_id, spec, status: "CREATED", created_at, …}`` into
      DynamoDB. The pipeline then moves status through ``running`` →
      ``succeeded``/``failed``.
    * Schedules :func:`app.mr_pipeline.run_job` on the current event
      loop and returns immediately.
    """
    org_id = payload.org_id
    spec = payload.spec

    # Fail fast when the org isn't registered. The pipeline would
    # otherwise mark the job ``failed`` after a full retrieval attempt,
    # which is a poor UX for a simple typo.
    registry: RegistryService = get_registry_service()
    try:
        await registry.get_org(org_id)
    except OrgNotFoundError as exc:
        raise NotFoundError(
            f"org '{org_id}' not found",
            details={"org_id": org_id},
        ) from exc
    except DynamoDBError as exc:
        log_event(
            log, "api.jobs.org_lookup_failed",
            "create_job: org lookup failed",
            level=logging.ERROR, exc_info=True,
            error_type=exc.__class__.__name__,
        )
        raise UpstreamError("failed to verify org") from exc

    jobs = JobsRepository()
    try:
        item = await jobs.create_job(
            {
                "org_id": org_id,
                "spec": spec,
                "status": "CREATED",
            }
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except DynamoDBError as exc:
        log_event(
            log, "api.jobs.create_failed",
            "create_job: DynamoDB write failed",
            level=logging.ERROR, exc_info=True,
            error_type=exc.__class__.__name__,
        )
        raise UpstreamError("failed to create job") from exc

    job_id = str(item.get("job_id") or "")
    if not job_id:
        # Defensive: create_job always stamps job_id via uuid4.
        raise UpstreamError("job created without id")

    # Create the artifact scaffold eagerly so the log file can be read
    # (empty but existing) immediately after POST even if the pipeline
    # hasn't started. Best-effort — a missing directory is fine; the
    # pipeline will re-run this at entry.
    try:
        await asyncio.to_thread(init_job_artifacts, job_id)
    except Exception as e:  # noqa: BLE001
        log.warning("api.jobs: init_job_artifacts failed: %s", e)

    with log_context(job_id=job_id, org_id=org_id):
        _schedule_pipeline(job_id)
        log_event(
            log, "api.jobs.created",
            "job created and pipeline scheduled",
            spec_len=len(spec),
        )

    return JobCreateResponse(job_id=job_id, status="CREATED")


class JobCancelResponse(BaseModel):
    """Response for ``POST /v1/jobs/{job_id}/cancel``."""

    model_config = ConfigDict(use_enum_values=True)

    job_id: JobId
    status: JobStatus = Field(default=JobStatus.CANCELLED)


# --------------------------------------------------------------------------- #
# POST /v1/jobs/{job_id}/cancel                                               #
# --------------------------------------------------------------------------- #


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobCancelResponse,
    summary="Cancel a running or queued job",
    responses={
        200: {"description": "Job marked cancelled; pipeline task stopped if in-process"},
        404: {"description": "job_id does not exist"},
        409: {"description": "Job already finished"},
        502: {"description": "DynamoDB failure"},
    },
)
async def cancel_job(job_id: JobId = Path(..., description="Job id")) -> JobCancelResponse:
    """Set job status to ``cancelled`` and cancel the in-process asyncio task.

    Ordering: persist ``cancelled`` first so a worker about to flip to
    ``running`` observes the signal; then :meth:`asyncio.Task.cancel`
    for the same-process pipeline. Other workers cannot cancel tasks
    they do not own.
    """
    jobs = JobsRepository()
    try:
        item = await jobs.get_job(job_id)
    except DynamoDBError as exc:
        log_event(
            log, "api.jobs.cancel_get_failed",
            "cancel_job: DynamoDB read failed",
            level=logging.ERROR, exc_info=True,
            error_type=exc.__class__.__name__,
        )
        raise UpstreamError("failed to fetch job") from exc

    if item is None:
        raise NotFoundError(
            f"job '{job_id}' not found",
            details={"job_id": job_id},
        )

    db_status = str(item.get("status") or "").strip().lower()
    if db_status in _TERMINAL_DB_STATUSES:
        raise ConflictError(
            "job has already finished and cannot be cancelled",
            details={"job_id": job_id, "status": api_status(db_status).value},
        )

    try:
        await jobs.update_job(job_id, {"status": "cancelled"})
    except DynamoDBError as exc:
        log_event(
            log, "api.jobs.cancel_update_failed",
            "cancel_job: DynamoDB write failed",
            level=logging.ERROR, exc_info=True,
            error_type=exc.__class__.__name__,
        )
        raise UpstreamError("failed to cancel job") from exc

    t = _pipeline_tasks.get(job_id)
    if t is not None and not t.done():
        t.cancel()

    with log_context(job_id=job_id):
        log_event(log, "api.jobs.cancelled", "job cancel requested")

    return JobCancelResponse(job_id=job_id, status=JobStatus.CANCELLED)


# --------------------------------------------------------------------------- #
# GET /v1/jobs/{job_id}                                                       #
# --------------------------------------------------------------------------- #


@router.get(
    "/jobs/{job_id}",
    response_model=Job,
    summary="Fetch a job by id (status, MR/staging URLs, log tail)",
    responses={
        200: {"description": "Job state returned"},
        404: {"description": "job_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def get_job(
    response: Response,
    job_id: JobId = Path(..., description="Job id as returned by POST /v1/jobs"),
    log_lines: int = Query(
        default=200,
        ge=0,
        le=5000,
        description="Max number of log lines to return (tail).",
    ),
) -> Job:
    """Return the :class:`~app.models.jobs.Job` record for ``job_id``.

    Composition:

    * ``job_id`` / ``org_id`` / ``spec`` / ``created_at`` — straight
      off the DynamoDB row.
    * ``status`` — uppercase public label (:class:`JobStatus`); the
      internal DB vocabulary is collapsed via :func:`api_status`.
    * ``mr_url`` / ``staging_url`` — the row's own fields, with a
      fallback to ``summary.json`` written by the pipeline (covers
      the race where the status flipped but the single combined
      update write hadn't committed yet, or old rows predating the
      field).
    * ``logs`` — a tail of the merged ``logs/*`` files under the
      job's artifact directory.
    """
    response.headers["Cache-Control"] = "no-store"

    jobs = JobsRepository()
    try:
        item = await jobs.get_job(job_id)
    except DynamoDBError as exc:
        log_event(
            log, "api.jobs.get_failed",
            "get_job: DynamoDB read failed",
            level=logging.ERROR, exc_info=True,
            error_type=exc.__class__.__name__,
        )
        raise UpstreamError("failed to fetch job") from exc

    if item is None:
        raise NotFoundError(
            f"job '{job_id}' not found",
            details={"job_id": job_id},
        )

    org_id = str(item.get("org_id") or "")
    db_status = str(item.get("status") or "")

    with log_context(job_id=job_id, org_id=org_id):
        try:
            summary: Dict[str, Any] = await asyncio.to_thread(read_summary, job_id) or {}
        except Exception as e:  # noqa: BLE001 — artifact read is advisory
            log.warning("api.jobs: read_summary failed: %s", e)
            summary = {}

        try:
            logs = await asyncio.to_thread(read_logs, job_id, max_lines=log_lines)
        except Exception as e:  # noqa: BLE001
            log.warning("api.jobs: read_logs failed: %s", e)
            logs = []

    # Prefer fields from the row; fall back to summary for legacy rows.
    mr_url = str(item.get("mr_url") or summary.get("mr_url") or "")
    staging_url = str(item.get("staging_url") or summary.get("staging_url") or "")

    return Job(
        job_id=job_id,
        org_id=org_id,
        spec=str(item.get("spec") or ""),
        status=api_status(db_status),
        mr_url=mr_url,
        staging_url=staging_url,
        logs=logs,
        created_at=item.get("created_at"),
    )
