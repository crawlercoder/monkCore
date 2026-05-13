"""Top-level API router.

Individual versioned routers are mounted here; `main.py` only has to
include this single aggregator.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import jobs, v1_router

api_router = APIRouter()
api_router.include_router(v1_router, prefix="/v1")
# Shorter paths: ``POST /api/jobs`` and ``GET /api/jobs/{job_id}`` match the
# same handlers as ``/api/v1/jobs`` (hidden from OpenAPI to avoid duplicate
# operations in the generated spec).
api_router.include_router(
    jobs.router,
    prefix="",
    tags=["jobs"],
    include_in_schema=False,
)
