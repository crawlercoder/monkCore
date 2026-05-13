"""RAG retriever abstraction.

A retriever takes a natural-language query and returns ranked chunks. The
transport (OpenSearch, pgvector, in-memory, …) is an implementation detail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Mapping


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    text: str
    score: float
    metadata: Mapping[str, str]


class Retriever(ABC):
    @abstractmethod
    async def retrieve(self, query: str, *, top_k: int = 8) -> List[RetrievedChunk]:
        raise NotImplementedError
