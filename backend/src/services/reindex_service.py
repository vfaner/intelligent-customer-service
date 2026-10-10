"""Background rebuild of the whole vector index after an embedding-model switch.

Why this exists
---------------
A vector collection is locked to the embedding model that created it:

* Most backends fix the vector dimension at creation, so a model with a
  different dimension makes every insert fail outright.
* Same dimension, different model inserts fine — but the two embedding spaces
  are incompatible and retrieval silently turns into noise.

There is no way to migrate vectors in place; the only correct response is to
delete them all and re-embed every document. This service does that as a
background job (a 9 000-chunk knowledge base takes a long time and eats vendor
quota), driven from the admin console:

    preflight  — embed one probe phrase with the *new* config; fail before
                 touching anything if credentials/endpoint are wrong.
    reset      — drop & recreate the collection, mark every document
                 un-indexed, stamp the new model signature.
    indexing   — re-run the normal per-document ingest with its existing
                 50-chunk resume and quota backoff.

The stamp happens *before* indexing, not after: if the process dies halfway,
a restart sees the new signature already recorded and runs in "repair" mode
(no second reset, just resume the unfinished documents), so a crash never
re-embeds completed chunks and never leaves two models' vectors in one
collection.
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..core.database import session_scope
from ..models.config import CSConfig
from ..models.document import Document
from ..utils.timeutil import utcnow_naive
from ..vector_store.factory import build_vector_store
from . import runtime
from .document_service import DocumentService
from .embedding_service import EmbeddingService, embedding_signature, load_embedding_config

log = logging.getLogger(__name__)

PROBE_TEXT = "连接测试"

# Mirrors scripts/ingest_retry.py: vendor embedding quota (ARK in particular)
# is a long-period budget, so a failed round waits before trying again.
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_MAX_ROUNDS = 100

# Error fragments that mean "try again later". Anything else (missing file,
# unsupported format, bad credentials) is deterministic — retrying just makes
# the user wait for nothing.
_TRANSIENT_HINTS = (
    "429", "rate", "quota", "限流", "频率", "额度", "超时", "timeout",
    "timed out", "502", "503", "504", "connection", "连接", "temporarily",
)

_SIG_KEYS = ("provider", "model", "base_url", "dim")


class ReindexInProgress(Exception):
    """Raised when a rebuild is requested while one is already running."""


def _now() -> str:
    return utcnow_naive().isoformat(timespec="seconds") + "Z"


def _fresh_state() -> Dict[str, Any]:
    return {
        "running": False,
        # idle | preflight | reset | indexing | done | failed
        "phase": "idle",
        "repair_mode": False,
        "docs_total": 0,
        "docs_done": 0,
        "chunks_done": 0,
        "chunks_total": 0,
        "current_file": "",
        "failed": [],
        "error": "",
        "started_at": "",
        "finished_at": "",
    }


_STATE: Dict[str, Any] = _fresh_state()
_STATE_LOCK = asyncio.Lock()
_current_task: Optional[asyncio.Task] = None


def _snapshot() -> Dict[str, Any]:
    return deepcopy(_STATE)


# ---------------------------------------------------------------------------
# CSConfig helpers (the row is a single JSON blob; always merge, never replace)
# ---------------------------------------------------------------------------
async def _mutate_config(mutate) -> None:
    async with session_scope() as session:
        row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
        if row is None:
            row = CSConfig(data={})
            session.add(row)
            await session.flush()
        data = dict(row.data or {})
        mutate(data)
        row.data = data


async def _stored_signature() -> Optional[Dict[str, Any]]:
    async with session_scope() as session:
        row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
        sig = (row.data or {}).get("index_signature") if row else None
        return dict(sig) if isinstance(sig, dict) else None


def signatures_equal(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return all(a.get(k) == b.get(k) for k in _SIG_KEYS)


def needs_rebuild(
    stored: Optional[Dict[str, Any]],
    current: Optional[Dict[str, Any]],
    ready_docs: int,
    store_dimension: Optional[int] = None,
) -> bool:
    """Server-side decision powering the console banner.

    Two independent signals say "rebuild":

    * the existing collection is dimension-locked to something other than the
      current model's output — this blocks inserts even when there are ZERO
      ready documents (a failed/retrying knowledge base still has a live
      collection on disk);
    * documents were indexed under a different model signature.

    With no lock mismatch and no ready documents there is nothing to rebuild
    yet: the first ingest stamps the signature itself.
    """
    current_dim = int((current or {}).get("dim") or 0)
    locked_dim = int(store_dimension or 0)
    if locked_dim and current_dim and locked_dim != current_dim:
        return True
    if ready_docs <= 0:
        return False
    if stored is None:
        return True
    return not signatures_equal(stored, current)


# ---------------------------------------------------------------------------
# Document-row helpers
# ---------------------------------------------------------------------------
async def _reset_all_documents() -> int:
    """Mark every document un-indexed. Files on disk are left untouched."""
    async with session_scope() as session:
        rows = (await session.execute(select(Document))).scalars().all()
        for r in rows:
            r.status = "uploaded"
            r.chunk_count = 0
            r.chunks_done = 0
            r.chunks_total = 0
            r.chunk_size = None
            r.chunk_overlap = None
            r.error_message = None
        return len(rows)


async def _refresh_progress(svc: DocumentService) -> List[Dict[str, Any]]:
    docs = await svc.list_documents()
    _STATE["docs_total"] = len(docs)
    _STATE["docs_done"] = sum(1 for d in docs if d["status"] == "ready")
    _STATE["chunks_done"] = sum(int(d.get("chunks_done") or 0) for d in docs)
    _STATE["chunks_total"] = sum(int(d.get("chunks_total") or 0) for d in docs)
    return docs


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def start_reindex(
    settings=None,
    *,
    cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> Dict[str, Any]:
    """Kick off a rebuild in the background and return immediately."""
    global _current_task
    async with _STATE_LOCK:
        # Check-and-flip must be atomic: two near-simultaneous requests would
        # otherwise both see running=False and start two rebuild jobs.
        if _STATE["running"]:
            raise ReindexInProgress("向量库重建任务已在运行中")
        _STATE.clear()
        _STATE.update(_fresh_state())
        _STATE.update(
            running=True, phase="preflight", started_at=_now(), finished_at=""
        )
        # create_task only schedules the coroutine — _run can't touch _STATE
        # until this coroutine next awaits, by which point the lock is out.
        _current_task = asyncio.create_task(
            _run(settings, cooldown=cooldown, max_rounds=max_rounds)
        )
    return _snapshot()


async def wait_until_done(timeout: float = 120.0) -> Dict[str, Any]:
    """Await the running job. Used by tests and nothing in production."""
    if _current_task is not None:
        await asyncio.wait_for(asyncio.shield(_current_task), timeout=timeout)
    return _snapshot()


async def _run(settings, *, cooldown: float, max_rounds: int) -> None:
    try:
        svc = DocumentService(settings)

        # --- preflight: new config must actually embed --------------------
        cfg = await load_embedding_config(settings)
        embedder = EmbeddingService(settings)
        try:
            vectors = await embedder.embed([PROBE_TEXT])
            if not vectors or not vectors[0]:
                raise RuntimeError("向量接口返回了空向量")
        finally:
            await embedder.aclose()
        dim = len(vectors[0])
        cfg["dim"] = dim
        sig = embedding_signature(cfg)

        stored = await _stored_signature()
        repair_mode = signatures_equal(stored, sig)
        _STATE["repair_mode"] = repair_mode

        # Persist the probed dimension so the UI/settings agree with reality.
        def _write_dims(data: Dict[str, Any]) -> None:
            # Synchronous on purpose: _mutate_config invokes the mutator
            # non-awaited inside its session.
            emb = dict(data.get("embedding") or {})
            emb["dim"] = dim
            data["embedding"] = emb
            vec = dict(data.get("vector_db") or {})
            vec["embedding_dimension"] = dim
            data["vector_db"] = vec

        await _mutate_config(_write_dims)

        if not repair_mode:
            # --- reset: drop the incompatible collection ------------------
            _STATE["phase"] = "reset"
            # Ephemeral store on purpose (blocking construction off the loop),
            # and any *cached* store handle now points at a dropped collection
            # — evict it so the next open rebuilds against the fresh one.
            store = await asyncio.to_thread(
                build_vector_store, settings, embedding_dimension_override=dim
            )
            try:
                await store.reset()
            finally:
                await store.aclose()
            runtime.invalidate("vector_store")
            await _reset_all_documents()
            # Stamp now: everything still in the collection is new-model from
            # this point on, which makes an interrupted run safely resumable.
            await _mutate_config(lambda data: data.__setitem__("index_signature", sig))
            log.warning("Vector index rebuild started: signature=%s", sig)
        else:
            log.info("Vector index rebuild in repair mode (resuming unfinished documents)")

        # --- indexing: every non-ready document, with quota backoff --------
        _STATE["phase"] = "indexing"
        await _refresh_progress(svc)
        async with session_scope() as session:
            rows = (await session.execute(select(Document))).scalars().all()
            pending = [
                {"id": r.id, "filename": r.filename, "file_path": r.file_path}
                for r in rows
                if r.status != "ready"
            ]
        failed: List[Dict[str, str]] = []

        for doc in pending:
            doc_id = doc["id"]
            filename = doc["filename"]
            _STATE["current_file"] = filename

            # A missing source file never recovers — don't burn 100 backoff
            # rounds waiting for quota that isn't the problem.
            if not Path(doc["file_path"]).exists():
                msg = f"原文件已丢失（{doc['file_path']}），请删除该文档记录后重新上传"
                failed.append({"filename": filename, "error": msg})
                log.warning("reindex skips %s: source file gone", filename)
                continue

            last_error = ""
            stalled = 0
            for rnd in range(1, max_rounds + 1):
                try:
                    await svc.process_document(doc_id)
                except Exception as e:  # quota / parse / vendor errors
                    last_error = str(e)[:500]
                    log.warning("reindex ingest round failed for %s: %s", filename, last_error)
                docs_by_id = {d["id"]: d for d in await _refresh_progress(svc)}
                cur = docs_by_id.get(doc_id)
                if cur and cur["status"] == "ready":
                    last_error = ""
                    break
                # Deterministic failures (bad file, parse error, auth) never
                # recover by waiting — fail fast instead of burning rounds.
                low = last_error.lower()
                transient = any(h in low for h in _TRANSIENT_HINTS)
                if not transient:
                    break
                wait = cooldown * min(4, 1 + stalled)
                stalled += 1
                log.info("reindex: %s not ready (round %d), waiting %.0fs", filename, rnd, wait)
                await asyncio.sleep(wait)

            if last_error:
                failed.append({"filename": filename, "error": last_error})
            await _refresh_progress(svc)

        _STATE["current_file"] = ""
        if failed:
            _STATE["phase"] = "failed"
            _STATE["failed"] = failed
            _STATE["error"] = f"{len(failed)} 个文档未完成，可点「重试」从断点继续"
            log.warning("Vector index rebuild finished with %d failed document(s)", len(failed))
        else:
            _STATE["phase"] = "done"
            _STATE["error"] = ""
            log.info("Vector index rebuild completed: %d documents", _STATE["docs_total"])
    except asyncio.CancelledError:
        _STATE["phase"] = "failed"
        _STATE["error"] = "重建任务被中断"
        raise
    except Exception as e:
        log.exception("vector index rebuild failed")
        _STATE["phase"] = "failed"
        _STATE["error"] = str(e)[:1000]
    finally:
        _STATE["running"] = False
        _STATE["finished_at"] = _now()


async def status_payload(settings=None) -> Dict[str, Any]:
    """Full status for ``GET /api/documents/reindex/status``.

    Cheap to poll: no embedding calls, only DB reads plus a best-effort count.
    """
    svc = DocumentService(settings)
    if _STATE["running"]:
        try:
            await _refresh_progress(svc)
        except Exception:
            pass

    out = _snapshot()
    docs = await svc.list_documents()
    ready = sum(1 for d in docs if d["status"] == "ready")
    out["docs_total"] = len(docs)
    out["docs_ready"] = ready

    cfg = await load_embedding_config(settings)
    current_sig = embedding_signature(cfg)
    stored = await _stored_signature()
    out["current_signature"] = current_sig
    out["index_signature"] = stored

    store_dim = None
    vector_count = None
    try:
        # Shared singleton, resolved exactly like the ingest path (DB-saved
        # dimension over .env): this endpoint is polled every few seconds
        # while a rebuild runs, and each poll used to open a whole new store
        # client (Chroma re-loading its DB / Qdrant re-checking the
        # collection). Not closed here — runtime.close_shared() owns it.
        store = await svc.open_vector_store()
        vector_count = await store.count()
        store_dim = await store.dimension()
    except Exception:
        pass
    out["vector_count"] = vector_count
    out["store_dimension"] = store_dim
    out["needs_rebuild"] = (
        False if out["running"] else needs_rebuild(stored, current_sig, ready, store_dim)
    )
    return out
