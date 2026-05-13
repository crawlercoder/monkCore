"""Continuous repository synchronization primitives.

Public surface re-exported here so callers can ``from app.sync import sync_repository``
without depending on the module layout below.
"""

from __future__ import annotations

from app.sync.repo_sync import (
    RepoSyncError,
    SyncErrorCode,
    sync_repository,
)

__all__ = [
    "RepoSyncError",
    "SyncErrorCode",
    "sync_repository",
]
