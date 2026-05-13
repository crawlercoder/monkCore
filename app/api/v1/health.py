"""Health + readiness endpoints.

* `/health`    — liveness. Cheap, always returns 200 if the process is up.
                  Used by load balancers and container runtimes.
* `/ready`     — readiness. Runs registered dependency probes (DB, Bedrock,
                  DynamoDB, …). Returns 503 if any required probe fails.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Response, status

from app import __version__
from app.api.deps import get_settings
from app.config import Settings
from app.models.health import HealthResponse, ReadinessResponse
from app.services.readiness import run_readiness_checks

router = APIRouter()

_STARTED_AT = time.monotonic()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        service=settings.app_name,
        version=__version__,
        environment=settings.environment.value,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={503: {"model": ReadinessResponse}},
)
async def ready(response: Response) -> ReadinessResponse:
    checks = await run_readiness_checks()
    all_ok = all(check.healthy for check in checks.values())
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ready" if all_ok else "degraded",
        checks=checks,
    )
