"""Process-wide singletons for the expensive runtime objects (perf batch).

``LLMService`` / ``EmbeddingService`` / ``RAGService`` deliberately re-read
CSConfig on every call so a console edit takes effect immediately. The price
was that every chat message paid for a fresh ``httpx.AsyncClient`` (full
TCP+TLS handshake to the LLM vendor), a fresh embedder client, and a fresh
vector-store client (Chroma re-opens its persistent DB; Qdrant/Milvus redo
their connection + collection checks).

These caches keep the instant-effect property: every entry is keyed by a
fingerprint of *all* config fields the object was built from, so any change
builds a new object right away. The displaced object is "retired" — closed
after a grace period so requests already holding it can finish.

Concurrency notes:

* Single event loop, so the check→build→recheck sequence needs no lock: the
  only interleaving point is the ``await build()``, and a duplicate build
  loses the recheck and is closed immediately (it was never handed out).
* Single process (same assumption as ``core.ratelimit`` and the reindex job
  state): one uvicorn worker, in-process caches.

Tests should call :func:`reset_shared` between cases so objects (and the
config they were built from) never leak across event loops.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# How long a displaced object stays open after a config change. Long enough
# for an in-flight LLM stream to finish, short enough that a console edit
# doesn't leave stale connections around forever.
RETIRE_DELAY = 60.0

# kind -> (fingerprint, object)
_SLOTS: Dict[str, Tuple[str, Any]] = {}


async def _safe_close(obj: Any) -> None:
    aclose = getattr(obj, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as e:  # closing is best-effort; never fatal
        log.debug("runtime: closing %s failed: %s", type(obj).__name__, e)


def _retire(obj: Any) -> None:
    """Close a displaced object after a grace period.

    Requests that fetched it before the config change may still be streaming
    through it, so it can't be closed synchronously. If the loop is already
    gone (interpreter shutdown), the object is simply dropped for the GC.
    """
    if getattr(obj, "aclose", None) is None:
        return

    async def _later() -> None:
        await asyncio.sleep(RETIRE_DELAY)
        await _safe_close(obj)

    try:
        asyncio.get_running_loop().create_task(_later())
    except RuntimeError:
        pass


async def get_shared(
    kind: str, fingerprint: str, build: Callable[[], Awaitable[Any]]
) -> Any:
    """Return the singleton for ``kind``, rebuilding when the config changed.

    ``build`` is only invoked on a cache miss; it must be a coroutine function
    (wrap cheap sync constructors in a trivial ``async def``).
    """
    cur = _SLOTS.get(kind)
    if cur is not None and cur[0] == fingerprint:
        return cur[1]

    obj = await build()

    # Recheck: another request may have populated the slot while we built.
    cur = _SLOTS.get(kind)
    if cur is not None and cur[0] == fingerprint:
        # Lost a build race — ours was never handed out, close it now.
        await _safe_close(obj)
        return cur[1]
    if cur is not None:
        _retire(cur[1])
    _SLOTS[kind] = (fingerprint, obj)
    return obj


def peek(kind: str) -> Optional[Any]:
    """Current cached object for ``kind`` (or None). For tests/diagnostics."""
    cur = _SLOTS.get(kind)
    return cur[1] if cur else None


def invalidate(kind: str) -> None:
    """Drop the cached object for ``kind`` (retired, not closed inline).

    Used when the underlying resource was rebuilt out from under us — e.g.
    the reindex pipeline drops and recreates the vector collection, so any
    cached store handle points at a dead collection.
    """
    cur = _SLOTS.pop(kind, None)
    if cur is not None:
        _retire(cur[1])


async def close_shared() -> None:
    """Close every cached object. Called from the app lifespan shutdown."""
    slots = list(_SLOTS.values())
    _SLOTS.clear()
    for _fp, obj in slots:
        await _safe_close(obj)


def reset_shared() -> None:
    """Drop all cached objects WITHOUT closing them. Test helper only.

    Tests run each case on its own event loop; an object built (and possibly
    retired) on a dead loop must not be awaited again, so they are just
    released for the GC.
    """
    _SLOTS.clear()
