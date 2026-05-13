"""Reusable FastAPI dependencies."""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from app.config import Settings, get_settings as _get_settings
from app.logging import request_id_var
from app.services.registry import RegistryService


def get_settings() -> Settings:
    """FastAPI-compatible wrapper around the cached Settings singleton."""
    return _get_settings()


def get_request_id() -> str:
    """Current request id (set by RequestContextMiddleware)."""
    return request_id_var.get()


@lru_cache(maxsize=1)
def get_registry_service() -> RegistryService:
    """Process-wide singleton ``RegistryService`` for endpoints.

    Safe to share: the service is stateless apart from boto3 clients,
    which are themselves cached and thread-safe for read/write calls.
    Tests can override this dep via ``app.dependency_overrides``.
    """
    return RegistryService()


SettingsDep = Depends(get_settings)
RequestIdDep = Depends(get_request_id)
RegistryServiceDep = Depends(get_registry_service)
