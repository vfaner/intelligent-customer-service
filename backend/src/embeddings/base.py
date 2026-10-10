"""Embedding adapter base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

# Single error type — see the note in vector_store/base.py. Embedder failures
# must reach the API exception handler as an AppError (502 `embedding_error`
# with the actionable message) instead of escaping as an anonymous 500.
from ..core.exceptions import EmbeddingError  # noqa: F401  (re-exported)


class BaseEmbedder(ABC):
    """Generate dense vectors for a list of strings."""

    dim: int = 0
    model: str = ""

    @abstractmethod
    async def embed(
        self, texts: List[str], *, rate_limit_budget: Optional[float] = None
    ) -> List[List[float]]:
        """Return one vector per input text. Order is preserved.

        ``rate_limit_budget`` caps how long 429 retries may burn on THIS call
        (seconds); ``None`` keeps the implementation default. Interactive
        query paths pass a small budget so a quota-exhausted vendor surfaces
        an error in seconds instead of holding the user's question hostage
        for the multi-minute ingestion budget.
        """
