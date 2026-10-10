"""Vector store base + shared types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Single error type for the whole stack: stores raise the SAME VectorStoreError
# the API exception handler understands (AppError → clean JSON with a
# `vector_store_error` code). This file used to define a plain-Exception
# lookalike, so store failures escaped the handler as anonymous 500s — and
# rag_service's `except VectorStoreError: raise` was dead code, catching the
# *other* class.
from ..core.exceptions import VectorStoreError  # noqa: F401  (re-exported)


@dataclass
class ChunkRecord:
    """A single chunk to be embedded and indexed."""
    chunk_id: str             # globally unique, e.g. {doc_id}:{index}
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    """A single retrieval hit."""
    chunk_id: str
    text: str
    score: float              # higher is more similar (0..1 after normalization)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseVectorStore(ABC):
    """Pluggable vector store. Implementations: Chroma, Qdrant."""

    name: str = "base"

    @abstractmethod
    async def add(
        self,
        document_id: str,
        chunks: List[ChunkRecord],
        vectors: List[List[float]],
    ) -> int:
        """Add (or upsert) chunks. Returns count indexed."""

    @abstractmethod
    async def query(
        self,
        vector: List[float],
        top_k: int = 5,
        threshold: float = 0.0,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[VectorHit]:
        """Return up to ``top_k`` hits above ``threshold``."""

    @abstractmethod
    async def delete_document(self, document_id: str) -> int:
        """Delete all chunks belonging to a document. Returns count removed."""

    @abstractmethod
    async def reset(self) -> None:
        """Drop and recreate the collection, discarding every stored vector.

        Needed when the embedding model changes: the collection's vector space
        (and, for most backends, its dimension) is fixed at creation time, so
        vectors produced by a different model cannot coexist with the old ones.
        """

    @abstractmethod
    async def count(self) -> int:
        """Total number of chunks indexed."""

    async def dimension(self) -> Optional[int]:
        """Vector dimension the *existing* collection is locked to, if known.

        A collection stays dimension-locked even when it holds zero vectors,
        so ``count()`` cannot answer "will a 1024-d insert succeed?". Backends
        that cannot tell return ``None`` (no signal, never an error): this is
        best-effort diagnostics for the rebuild banner, not a hard contract.
        """
        return None

    @abstractmethod
    async def aclose(self) -> None:
        """Release any held resources."""
