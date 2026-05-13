"""Repo endpoints.

* ``POST /v1/repo/register``            — register a repo under an org.
  Idempotent on ``(org_id, canonical_url)``: submitting the same repo
  twice returns the existing ``repo_id`` (HTTP 200 with
  ``reused=true``) instead of minting a second row. This stops the
  worker from re-cloning and re-embedding a repo that already has
  chunks in the org's vector store.
* ``GET  /v1/repos/{repo_id}``          — fetch a repo's metadata + status.
* ``POST /v1/repo/process/{repo_id}``   — manually trigger ingestion
  (testing / operator recovery).

The register and process endpoints are thin wrappers over
:class:`RegistryService` and :func:`process_repo` respectively; all of
the interesting logic (id generation, FK validation, status transitions,
clone orchestration) lives in the service / worker layers.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_registry_service
from app.db.dynamodb import DynamoDBError
from app.db.orgs import OrgNotFoundError
from app.db.repos import RepoAlreadyExistsError, RepoNotFoundError
from app.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UpstreamError,
)
from app.logging import get_logger, log_context, log_event
from app.models.orgs import OrgId
from app.models.repos import BranchName, Repo, RepoId, RepoStatus, RepoUrl
from app.services.git_clone import GitCloneError
from app.services.registry import RegistryService
from app.services.secrets import SecretsManagerError
from app.workers.repo_ingest import RepoIngestSkipped, process_repo

log = get_logger(__name__)

router = APIRouter()


class RepoRegisterRequest(BaseModel):
    """Payload for ``POST /repo/register``.

    URL and branch constraints live on the :mod:`app.models.repos` types
    so both the HTTP layer and the DynamoDB layer enforce identical
    rules — one source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    repo_url: RepoUrl = Field(..., description="Git URL (https or ssh)")
    org_id: OrgId = Field(..., description="Owning organization id")
    branch: BranchName = Field(
        default="main",
        description="Branch to track (defaults to main)",
    )


class RepoRegisterResponse(BaseModel):
    """Response for ``POST /repo/register``.

    ``reused`` is ``True`` when a repo with the same canonical URL
    already existed under this org: the server returned its existing
    ``repo_id`` and left the row status untouched (no re-clone, no
    re-embed). HTTP status is 200 in that case, 201 for a brand-new
    registration.
    """

    repo_id: RepoId
    repo_url: RepoUrl
    org_id: OrgId
    branch: BranchName
    status: RepoStatus
    created_at: datetime
    reused: bool = False


@router.post(
    "/repo/register",
    response_model=RepoRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a repo under an existing org (idempotent on URL)",
    responses={
        200: {"description": "Repo already registered for this org — returning existing id"},
        201: {"description": "Repo registered"},
        400: {"description": "Invalid input"},
        404: {"description": "org_id does not exist"},
        409: {"description": "Generated id already exists (retry)"},
        502: {"description": "DynamoDB failure"},
    },
)
async def register_repo(
    payload: RepoRegisterRequest,
    response: Response,
    registry: RegistryService = Depends(get_registry_service),
) -> RepoRegisterResponse:
    # Bind org_id up front so "org not found" / "upstream failure"
    # logs all reference the right org.
    with log_context(org_id=payload.org_id):
        try:
            repo, reused = await registry.register_or_get_repo(
                payload.repo_url,
                payload.org_id,
                branch=payload.branch,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        except OrgNotFoundError as exc:
            raise NotFoundError(
                f"org '{payload.org_id}' not found",
                details={"org_id": payload.org_id},
            ) from exc
        except RepoAlreadyExistsError as exc:
            raise ConflictError("repo id already exists, retry") from exc
        except DynamoDBError as exc:
            log_event(log, "api.repo.register_failed",
                      "register_repo: upstream failure",
                      level=logging.ERROR, exc_info=True)
            raise UpstreamError("failed to register repo") from exc

        with log_context(repo_id=repo.repo_id):
            if reused:
                log_event(log, "api.repo.reused",
                          "repo already registered — returning existing id",
                          branch=repo.branch, status=repo.status.value)
            else:
                log_event(log, "api.repo.registered", "repo registered",
                          branch=repo.branch, status=repo.status.value)

        response.status_code = (
            status.HTTP_200_OK if reused else status.HTTP_201_CREATED
        )

        return RepoRegisterResponse(
            repo_id=repo.repo_id,
            repo_url=repo.repo_url,
            org_id=repo.org_id,
            branch=repo.branch,
            status=repo.status,
            created_at=repo.created_at,
            reused=reused,
        )


@router.get(
    "/repos/{repo_id}",
    response_model=Repo,
    summary="Fetch a repo by id (debug)",
    responses={
        404: {"description": "repo_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def get_repo(
    repo_id: RepoId = Path(..., description="Repository id"),
    registry: RegistryService = Depends(get_registry_service),
) -> Repo:
    """Return the full repo row, including its current lifecycle status."""
    with log_context(repo_id=repo_id):
        try:
            return await registry.get_repo(repo_id)
        except RepoNotFoundError as exc:
            raise NotFoundError(
                f"repo '{repo_id}' not found",
                details={"repo_id": repo_id},
            ) from exc
        except DynamoDBError as exc:
            log_event(log, "api.repo.get_failed",
                      "get_repo: upstream failure",
                      level=logging.ERROR, exc_info=True)
            raise UpstreamError("failed to fetch repo") from exc


@router.post(
    "/repo/process/{repo_id}",
    response_model=Repo,
    summary="Trigger repo ingestion manually (debug)",
    responses={
        200: {"description": "Already READY or newly READY"},
        404: {"description": "repo_id does not exist"},
        409: {"description": "Another worker is already processing this repo"},
        502: {"description": "Clone / DynamoDB / Secrets Manager failure"},
        504: {"description": "Clone exceeded the configured timeout"},
    },
)
async def process_repo_endpoint(
    repo_id: RepoId = Path(..., description="Repository id to ingest"),
) -> Repo:
    """Synchronously run the repo-ingest worker for ``repo_id``.

    Intended for local testing and operator recovery. The call blocks
    until the clone finishes (up to ``clone_service.timeout_seconds`` per
    attempt, multiplied by the retry count — several minutes in the worst
    case). Don't put this behind an ALB/ingress with a short idle
    timeout; use the background worker path for production.

    Semantics
    ---------
    * Normal case — returns the repo with ``status=READY``.
    * ``status=READY`` on arrival — the worker short-circuits and this
      endpoint returns the existing row unchanged (idempotent 200).
    * Another worker holds ``CLONING`` — returns **409 Conflict**; retry
      or inspect via ``GET /repos/{repo_id}``.
    * Any downstream failure — returns **502** (or **504** for timeouts)
      with the specific error class in ``details.error_type``.
    """
    with log_context(repo_id=repo_id):
        log_event(log, "api.repo.process_requested",
                  "manual ingestion triggered via API")
        try:
            return await process_repo(repo_id)
        except RepoNotFoundError as exc:
            raise NotFoundError(
                f"repo '{repo_id}' not found",
                details={"repo_id": repo_id},
            ) from exc
        except RepoIngestSkipped as skip:
            if skip.current_status is RepoStatus.READY:
                # "Nothing to do" is a successful debug response — 200
                # with the current row so the operator can confirm state.
                try:
                    return await get_registry_service().get_repo(repo_id)
                except Exception as exc:  # pragma: no cover
                    log_event(log, "api.repo.process_refetch_failed",
                              "failed to re-fetch after skip",
                              level=logging.ERROR, exc_info=True)
                    raise UpstreamError("failed to fetch repo after skip") from exc
            raise ConflictError(
                f"repo '{repo_id}' is already being processed",
                details={"repo_id": repo_id, "reason": skip.reason},
            ) from skip
        except GitCloneError as exc:
            # Every subclass (auth, not-found, branch, timeout, conflict,
            # transient) ends up here. We surface the class name so
            # debug clients can distinguish without parsing the message.
            err_type = exc.__class__.__name__
            http_status = (
                status.HTTP_504_GATEWAY_TIMEOUT
                if err_type == "GitCloneTimeoutError"
                else status.HTTP_502_BAD_GATEWAY
            )
            log_event(log, "api.repo.process_git_failed",
                      f"git failure ({err_type}): {exc}",
                      level=logging.WARNING,
                      error_type=err_type, http_status=http_status)
            raise UpstreamError(
                f"git operation failed: {exc}",
                details={"repo_id": repo_id, "error_type": err_type},
                status_code=http_status,
            ) from exc
        except (DynamoDBError, SecretsManagerError) as exc:
            log_event(log, "api.repo.process_upstream_failed",
                      "process_repo: upstream failure",
                      level=logging.ERROR, exc_info=True,
                      error_type=exc.__class__.__name__)
            raise UpstreamError(
                f"repo ingestion failed: {exc}",
                details={"repo_id": repo_id, "error_type": exc.__class__.__name__},
            ) from exc


