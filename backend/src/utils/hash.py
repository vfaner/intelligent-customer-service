"""File hashing for dedup."""
from __future__ import annotations

import hashlib
from pathlib import Path


def md5_file(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
