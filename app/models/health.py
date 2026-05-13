"""Health + readiness response models."""

from __future__ import annotations

from typing import Dict, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    uptime_seconds: float = Field(ge=0)


class ReadinessCheck(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"] = "ready"
    checks: Dict[str, ReadinessCheck]
