"""Time helpers shared across services."""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Naive UTC now — the replacement for the deprecated ``datetime.utcnow()``.

    Naive on purpose: the ORM columns are ``DateTime`` without ``timezone=True``
    (matching SQLite's ``func.now()``), and cutoff comparisons against them
    would raise ``TypeError`` if one side were timezone-aware. Stripping the
    tzinfo from an aware ``now()`` yields exactly the value ``utcnow()`` used
    to return. ``datetime.now()`` (local time) must NOT be used for these
    columns — it would silently shift timestamps by the server's UTC offset.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
