"""Org endpoints.

* ``POST /v1/org/create``              — create an org and store its GitLab token.
  Idempotent on ``name``: re-submitting the same org name rotates the
  stored token and returns the existing ``org_id`` (HTTP 200 with
  ``reused=true``) instead of minting a new org. Keeps every customer
  mapped to a single vector store at ``./vector_store/{org_id}/``.
* ``PUT  /v1/orgs/{org_id}/token``     — rotate the GitLab token for an
  existing org.
* ``GET  /v1/orgs``                    — list orgs (debug / UI dropdown).
* ``GET  /v1/orgs/{org_id}``           — fetch an org's metadata (debug).
* ``GET  /v1/orgs/{org_id}/repos``     — list repos owned by an org.
* ``GET  /v1/orgs/{org_id}/jobs``      — list jobs for that org (newest first).

Errors map to the standard :mod:`app.errors` envelope:
* 400 — invalid input that passed Pydantic but failed service checks.
* 404 — org_id does not exist (``GET`` / token-update).
* 409 — race condition on the generated id (extremely rare with uuid4).
* 422 — Pydantic validation failure on the request body / path.
* 502 — downstream failure (DynamoDB, Secrets Manager).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Path, Query, Response, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.api.deps import get_registry_service
from app.db.dynamodb import DynamoDBError, JobsRepository
from app.db.orgs import OrgAlreadyExistsError, OrgNotFoundError
from app.errors import BadRequestError, ConflictError, NotFoundError, UpstreamError
from app.logging import get_logger, log_context, log_event
from app.models.jobs import JobSummary, api_status
from app.models.orgs import Org, OrgId, OrgName, SecretName
from app.models.repos import Repo
from app.services.registry import RegistryService
from app.services.secrets import SecretsManagerError

log = get_logger(__name__)

router = APIRouter()


class OrgCreateRequest(BaseModel):
    """Payload for ``POST /org/create``.

    Canonical field is ``gitlab_token`` to match backend internals, but
    we also accept ``github_token`` as an input alias for compatibility
    with older clients / docs.
    """

    model_config = ConfigDict(extra="forbid")

    name: OrgName = Field(..., description="Human-readable org name")
    gitlab_token: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="GitLab personal / group / deploy access token",
        validation_alias=AliasChoices("gitlab_token", "github_token"),
        # Keep the token out of every auto-generated example / log line.
        repr=False,
    )


class OrgCreateResponse(BaseModel):
    """Response for ``POST /org/create``.

    The token itself is never echoed back — only the generated ``org_id``
    and the ``secret_name`` so clients can correlate with Secrets Manager
    if needed.

    ``reused`` is ``True`` when an org with the same (normalized) name
    already existed: the server rotated the stored token to the one in
    the request and returned the existing ``org_id``. HTTP status is
    200 in that case, 201 for a brand-new provision.
    """

    org_id: OrgId
    name: OrgName
    secret_name: SecretName
    created_at: datetime
    reused: bool = False


class OrgTokenUpdateRequest(BaseModel):
    """Payload for ``PUT /orgs/{org_id}/token``."""

    model_config = ConfigDict(extra="forbid")

    gitlab_token: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="New GitLab personal / group / deploy access token",
        validation_alias=AliasChoices("gitlab_token", "github_token"),
        repr=False,
    )


class OrgTokenUpdateResponse(BaseModel):
    """Response for ``PUT /orgs/{org_id}/token``."""

    org_id: OrgId
    secret_name: SecretName
    rotated_at: datetime


@router.post(
    "/org/create",
    response_model=OrgCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an org (idempotent on name) and store its GitLab token",
    responses={
        200: {"description": "Org with this name already existed — token rotated"},
        201: {"description": "Org provisioned"},
        400: {"description": "Invalid input"},
        409: {"description": "Generated id already exists (retry)"},
        502: {"description": "DynamoDB or Secrets Manager failure"},
    },
)
async def create_org(
    payload: OrgCreateRequest,
    response: Response,
    registry: RegistryService = Depends(get_registry_service),
) -> OrgCreateResponse:
    try:
        org, reused = await registry.create_or_get_org(
            payload.name, payload.gitlab_token,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except OrgAlreadyExistsError as exc:
        # uuid4 collision on the generated id (extremely rare) or a
        # retried request that raced past the find_by_name check —
        # surface as 409 so the client can retry.
        raise ConflictError("org id already exists, retry") from exc
    except (DynamoDBError, SecretsManagerError) as exc:
        log_event(log, "api.org.create_failed",
                  "create_org: upstream failure",
                  level=logging.ERROR,
                  exc_info=True,
                  error_type=exc.__class__.__name__)
        raise UpstreamError("failed to provision org") from exc

    # Bind the resolved org_id for the remainder of the request — any
    # subsequent log line (including middleware's http.request) carries
    # it automatically.
    with log_context(org_id=org.org_id):
        if reused:
            log_event(
                log, "api.org.reused",
                "org already existed — rotated token and returned existing id",
                org_name=org.name, secret_name=org.secret_name,
            )
        else:
            log_event(
                log, "api.org.created", "org created",
                org_name=org.name, secret_name=org.secret_name,
            )

    # 200 OK for "reused", 201 Created for a genuinely new row. The
    # response body's ``reused`` flag carries the same signal for
    # clients that can't branch on status code.
    response.status_code = status.HTTP_200_OK if reused else status.HTTP_201_CREATED

    return OrgCreateResponse(
        org_id=org.org_id,
        name=org.name,
        secret_name=org.secret_name,
        created_at=org.created_at,
        reused=reused,
    )


@router.put(
    "/orgs/{org_id}/token",
    response_model=OrgTokenUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate the GitLab token for an existing org",
    responses={
        400: {"description": "Invalid input"},
        404: {"description": "org_id does not exist"},
        502: {"description": "DynamoDB or Secrets Manager failure"},
    },
)
async def update_org_token(
    payload: OrgTokenUpdateRequest,
    org_id: OrgId = Path(..., description="Organization id"),
    registry: RegistryService = Depends(get_registry_service),
) -> OrgTokenUpdateResponse:
    """Update (rotate) the GitLab token stored for ``org_id``.

    The token is written to the same secret name that was created at
    ``POST /org/create`` time, so every worker that looks up the token
    via the org_id picks up the new value on the next read (cache TTL
    applies; the TTL cache is invalidated synchronously on write).
    """
    with log_context(org_id=org_id):
        try:
            # Validate existence first so a missing org produces 404 even
            # when the secret-write path would have silently succeeded.
            org = await registry.get_org(org_id)
            secret_name = await registry.update_org_token(
                org_id, payload.gitlab_token,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        except OrgNotFoundError as exc:
            raise NotFoundError(
                f"org '{org_id}' not found",
                details={"org_id": org_id},
            ) from exc
        except (DynamoDBError, SecretsManagerError) as exc:
            log_event(
                log, "api.org.token_update_failed",
                "update_org_token: upstream failure",
                level=logging.ERROR, exc_info=True,
                error_type=exc.__class__.__name__,
            )
            raise UpstreamError("failed to rotate gitlab token") from exc

        log_event(
            log, "api.org.token_rotated",
            "org gitlab token rotated via API",
            secret_name=secret_name,
        )
        return OrgTokenUpdateResponse(
            org_id=org.org_id,
            secret_name=secret_name,
            rotated_at=datetime.now(timezone.utc),
        )


@router.get(
    "/orgs",
    response_model=List[Org],
    summary="List orgs (debug)",
    responses={502: {"description": "DynamoDB failure"}},
)
async def list_orgs(
    limit: int = Query(default=100, ge=1, le=1000),
    registry: RegistryService = Depends(get_registry_service),
) -> List[Org]:
    """Return up to ``limit`` orgs (unordered).

    Backed by a bounded DynamoDB ``Scan``. Fine while the number of orgs
    is small; swap for a GSI-driven listing if the table grows.
    """
    try:
        return await registry.list_orgs(limit=limit)
    except DynamoDBError as exc:
        log.exception("list_orgs: upstream failure")
        raise UpstreamError("failed to list orgs") from exc


@router.get(
    "/orgs/{org_id}",
    response_model=Org,
    summary="Fetch an org by id (debug)",
    responses={
        404: {"description": "org_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def get_org(
    org_id: OrgId = Path(..., description="Organization id"),
    registry: RegistryService = Depends(get_registry_service),
) -> Org:
    """Return the full org row as stored in DynamoDB.

    The token itself is never returned — only the pointer
    (``secret_name``) that resolves to Secrets Manager.
    """
    with log_context(org_id=org_id):
        try:
            return await registry.get_org(org_id)
        except OrgNotFoundError as exc:
            raise NotFoundError(
                f"org '{org_id}' not found",
                details={"org_id": org_id},
            ) from exc
        except DynamoDBError as exc:
            log_event(log, "api.org.get_failed",
                      "get_org: upstream failure",
                      level=__import__("logging").ERROR, exc_info=True)
            raise UpstreamError("failed to fetch org") from exc


@router.get(
    "/orgs/{org_id}/repos",
    response_model=List[Repo],
    summary="List repos for an org",
    responses={
        404: {"description": "org_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def list_repos_for_org(
    org_id: OrgId = Path(..., description="Organization id"),
    limit: int = Query(default=100, ge=1, le=1000),
    registry: RegistryService = Depends(get_registry_service),
) -> List[Repo]:
    """Return repos for ``org_id`` via the ``org_id-index`` GSI.

    Validates that the org exists first so a missing id is a clean 404
    rather than an empty list (which ``list_repos_by_org`` would
    otherwise return — the GSI can't tell absent from empty).
    """
    with log_context(org_id=org_id):
        try:
            await registry.get_org(org_id)
            return await registry.list_repos_by_org(org_id, limit=limit)
        except OrgNotFoundError as exc:
            raise NotFoundError(
                f"org '{org_id}' not found",
                details={"org_id": org_id},
            ) from exc
        except DynamoDBError as exc:
            log_event(log, "api.org.list_repos_failed",
                      "list_repos_for_org: upstream failure",
                      level=__import__("logging").ERROR, exc_info=True)
            raise UpstreamError(
                "failed to list repos",
                details={
                    "dynamodb": str(exc)[:2000],
                    "iam_hint": (
                        "List-by-org uses Query on the GSI org_id-index. Add "
                        "dynamodb:Query on the index ARN (e.g. "
                        "arn:...:table/repos/index/org_id-index), not only the "
                        "table ARN."
                    ),
                },
            ) from exc


@router.get(
    "/orgs/{org_id}/jobs",
    response_model=List[JobSummary],
    summary="List jobs for an org",
    responses={
        404: {"description": "org_id does not exist"},
        502: {"description": "DynamoDB failure"},
    },
)
async def list_jobs_for_org(
    org_id: OrgId = Path(..., description="Organization id"),
    limit: int = Query(default=100, ge=1, le=1000),
    registry: RegistryService = Depends(get_registry_service),
) -> List[JobSummary]:
    """Return recent jobs for ``org_id`` (newest first).

    Backed by a filtered :class:`~boto3.resources.factory.dynamodb.Table.scan`
    on the ``jobs`` table. Validates org existence (404) first.
    """
    jobs = JobsRepository()
    with log_context(org_id=org_id):
        try:
            await registry.get_org(org_id)
            rows = await jobs.list_jobs_by_org(org_id, limit=limit)
        except OrgNotFoundError as exc:
            raise NotFoundError(
                f"org '{org_id}' not found",
                details={"org_id": org_id},
            ) from exc
        except DynamoDBError as exc:
            log_event(
                log, "api.org.list_jobs_failed",
                "list_jobs_for_org: upstream failure",
                level=__import__("logging").ERROR, exc_info=True,
            )
            raise UpstreamError("failed to list jobs") from exc

    out: list[JobSummary] = []
    for item in rows:
        jid = str(item.get("job_id") or "")
        if not jid:
            continue
        ca = item.get("created_at")
        if not ca:
            continue
        out.append(
            JobSummary(
                job_id=jid,
                org_id=str(item.get("org_id") or org_id),
                spec=str(item.get("spec") or ""),
                status=api_status(item.get("status")),
                mr_url=str(item.get("mr_url") or ""),
                staging_url=str(item.get("staging_url") or ""),
                created_at=ca,
            )
        )
    return out
