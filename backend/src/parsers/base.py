"""Document parser base class + errors."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class DocumentParseError(Exception):
    """Raised when a document cannot be parsed."""


class Parser(ABC):
    code: str = "base"
    display_name: str = "Base"
    extensions: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, path: str | Path) -> str:
        """Return the document's plain text."""
