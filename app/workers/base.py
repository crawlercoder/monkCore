"""Base event handler used by every worker."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from pydantic import BaseModel


class Event(BaseModel):
    """Minimal envelope matching our EventBridge contract."""

    version: str = "1"
    id: str
    source: str
    detail_type: str
    workflow_id: str
    correlation_id: str
    idempotency_key: str
    detail: Mapping[str, Any]


class EventHandler(ABC):
    """Subclasses declare `source` + `detail_type` they care about."""

    source: str
    detail_type: str

    def matches(self, event: Event) -> bool:
        return event.source == self.source and event.detail_type == self.detail_type

    @abstractmethod
    async def handle(self, event: Event) -> None:
        """Process an event. Must be idempotent on `event.idempotency_key`."""
        raise NotImplementedError
