"""v1 API."""

from fastapi import APIRouter

from app.api.v1 import health, jobs, orgs, repos

v1_router = APIRouter()
v1_router.include_router(health.router, tags=["health"])
v1_router.include_router(orgs.router, tags=["orgs"])
v1_router.include_router(repos.router, tags=["repos"])
v1_router.include_router(jobs.router, tags=["jobs"])

__all__ = ["v1_router"]
