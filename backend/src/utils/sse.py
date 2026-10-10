"""Server-Sent Events formatting helpers."""
from __future__ import annotations

import json
from typing import Any, Optional


def sse_pack(event: str, data: Any, *, id: Optional[str] = None) -> bytes:
    """Encode a single SSE event as bytes (UTF-8)."""
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, default=str)
    lines = ["event: " + event]
    if id:
        lines.append("id: " + id)
    for line in data.splitlines() or [""]:
        lines.append("data: " + line)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def make_keeper() -> bytes:
    """A no-op SSE comment to keep the connection alive through proxies."""
    return b": keep-alive\n\n"
