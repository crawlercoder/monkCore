"""Agent abstraction.

Every agent implements `run(input)` and returns a typed output. Keeping
the interface narrow makes agents easy to mock, swap models, and chain
from a service or a worker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class Agent(ABC, Generic[InputT, OutputT]):
    """Base class for AI agents."""

    name: str = "agent"

    @abstractmethod
    async def run(self, input: InputT) -> OutputT:
        """Execute the agent. Must be idempotent for the same input."""
        raise NotImplementedError
