"""Document service: upload → parse → chunk → embed → store."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import Settings, get_settings
from ..core.database import session_scope
from ..core.exceptions import DocumentError
from ..core.registry import parser_for_filename
from ..models.document import Document
from ..utils.hash import md5_file
from ..utils.text_splitter import TextChunk, split_text
from ..vector_store.base import BaseVectorStore, ChunkRecord
from ..vector_store.factory import get_shared_vector_store
from .embedding_service import EmbeddingService

log = logging.getLogger(__name__)


class DocumentService:
    # How many chunks to embed + index per round trip. Small enough that the
    # progress bar moves and an interruption loses little work; large enough
    # not to add meaningful overhead.
    INDEX_SLICE = 50
    # A document stuck in `processing` for longer than this had its worker die
    # (server restart, kill, crash) — nothing will ever finish it.
    STALE_PROCESSING_MINUTES = 15

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings
        self._vector_store: Optional[BaseVectorStore] = None
        self._embedder: Optional[EmbeddingService] = None

    async def _set_progress(self, document_id: str, *, done: int, total: int) -> None:
        """Persist ingestion progress so the UI can poll it."""
        from sqlalchemy import select

        try:
            async with session_scope() as session:
                doc = (
                    await session.execute(select(Document).where(Document.id == document_id))
                ).scalar_one_or_none()
                if doc is not None:
                    doc.chunks_done = done
                    doc.chunks_total = total
        except Exception as e:      # progress is best-effort, never fatal
            log.debug("could not persist progress for %s: %s", document_id, e)

    async def _maybe_stamp_index_signature(self, actual_dim: int) -> None:
        """Backfill ``index_signature`` on a pre-feature knowledge base.

        Called after a document ingests from chunk 0 with no stamp yet. The
        rebuild pipeline owns the signature otherwise, so an existing one is
        never touched here.
        """
        try:
            from .embedding_service import embedding_signature, load_embedding_config
            from .reindex_service import _mutate_config, _stored_signature

            if await _stored_signature() is not None:
                return
            cfg = await load_embedding_config(self._settings)
            cfg["dim"] = int(actual_dim)
            sig = embedding_signature(cfg)
            await _mutate_config(lambda data: data.setdefault("index_signature", sig))
            log.info("Backfilled index signature after ingest: %s", sig)
        except Exception as e:  # best-effort bookkeeping, never fatal to ingest
            log.debug("could not backfill index signature: %s", e)

    # ---- accessors --------------------------------------------------
    def settings(self) -> Settings:
        return self._settings or get_settings()

    async def open_vector_store(self) -> BaseVectorStore:
        """The shared vector store, honouring the DB-saved dimension.

        The console (and the rebuild pipeline) persist the probed embedding
        dimension into ``CSConfig.vector_db.embedding_dimension``; qdrant /
        milvus fix the dimension at collection creation, so ingesting through
        a store built from the .env value would upsert wrong-dim vectors into
        the recreated collection. Chroma ignores the override (its dimension is
        inferred from the first insert).

        The store itself is a process-wide singleton keyed by its config
        (``vector_store.factory.get_shared_vector_store``) — construction does
        blocking network/disk I/O that used to run per request on the event
        loop.
        """
        if self._vector_store is not None:
            return self._vector_store
        override = None
        try:
            from ..models.config import fetch_config_row

            async with session_scope() as session:
                row = await fetch_config_row(session)
                vec = (row.data or {}).get("vector_db") if row else None
                if vec and vec.get("embedding_dimension"):
                    override = int(vec["embedding_dimension"])
        except Exception as e:
            log.debug("could not read vector dim from DB, using .env: %s", e)
        self._vector_store = await get_shared_vector_store(
            self._settings, embedding_dimension_override=override
        )
        return self._vector_store

    def embedder(self) -> EmbeddingService:
        if self._embedder is None:
            self._embedder = EmbeddingService(self._settings)
        return self._embedder

    # ---- upload -----------------------------------------------------
    async def save_upload(
        self,
        filename: str,
        content: bytes,
        *,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save the upload to disk and persist a Document row."""
        if not filename:
            raise DocumentError("filename is empty")
        spec = parser_for_filename(filename)
        if spec is None:
            raise DocumentError(f"Unsupported file type: {filename}")

        s = self.settings()
        upload_dir = Path(s.app.upload_dir).resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)

        doc_id = uuid.uuid4().hex
        safe_name = Path(filename).name
        stored_path = upload_dir / f"{doc_id}_{safe_name}"
        # Up to MAX_UPLOAD_MB of disk I/O + hashing — keep the event loop free
        # so chat streams don't stutter while a file lands.
        await asyncio.to_thread(stored_path.write_bytes, content)
        file_md5 = await asyncio.to_thread(md5_file, stored_path)

        from sqlalchemy import select

        async with session_scope() as session:
            existing = (
                await session.execute(select(Document).where(Document.file_md5 == file_md5))
            ).scalar_one_or_none()
            if existing is not None:
                try:
                    stored_path.unlink()
                except OSError:
                    pass
                return existing.to_dict()

            doc = Document(
                id=doc_id,
                filename=safe_name,
                file_path=str(stored_path),
                file_size=len(content),
                file_md5=file_md5,
                mime_type=mime_type,
                parser_code=spec.code,
                status="uploaded",
                chunk_count=0,
            )
            session.add(doc)
            await session.flush()
            return doc.to_dict()

    async def _rag_options(
        self, chunk_size: Optional[int], chunk_overlap: Optional[int]
    ) -> Dict[str, Any]:
        """Resolve chunking options: explicit args → CSConfig → .env defaults."""
        s = self.settings()
        opts: Dict[str, Any] = {
            "chunk_size": s.rag.chunk_size,
            "chunk_overlap": s.rag.chunk_overlap,
            "strategy": s.rag.chunk_strategy,
            "prefix_heading": s.rag.chunk_prefix_heading,
        }
        try:
            from ..models.config import fetch_config_row

            async with session_scope() as session:
                row = await fetch_config_row(session)
                rag = (row.data or {}).get("rag") if row else None
            if rag:
                if rag.get("chunk_size"):
                    opts["chunk_size"] = int(rag["chunk_size"])
                if rag.get("chunk_overlap") is not None:
                    opts["chunk_overlap"] = int(rag["chunk_overlap"])
                if rag.get("chunk_strategy"):
                    opts["strategy"] = rag["chunk_strategy"]
                if rag.get("chunk_prefix_heading") is not None:
                    opts["prefix_heading"] = bool(rag["chunk_prefix_heading"])
        except Exception as e:
            log.debug("could not read rag config from DB: %s", e)

        # Explicit per-request overrides win.
        if chunk_size:
            opts["chunk_size"] = int(chunk_size)
        if chunk_overlap is not None:
            opts["chunk_overlap"] = int(chunk_overlap)
        # Guard against a saved overlap >= size, which the splitter rejects.
        if opts["chunk_overlap"] >= opts["chunk_size"]:
            opts["chunk_overlap"] = max(0, opts["chunk_size"] // 5)
        return opts

    # ---- processing -------------------------------------------------
    async def fetch_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        """Current row as a dict, or None. Used by the API to answer right
        after a background ingest is queued."""
        from sqlalchemy import select

        async with session_scope() as session:
            doc = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            return doc.to_dict() if doc is not None else None

    async def process_document(
        self,
        document_id: str,
        *,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        on_started: Optional[asyncio.Event] = None,
    ) -> Dict[str, Any]:
        """Parse → chunk → embed → index one document.

        ``on_started`` is set as soon as the row has been flipped to
        ``processing`` (or the call returned early), letting the API answer
        a background-ingest request with a truthful status without waiting
        for the (minutes-long) ingestion itself.
        """
        opts = await self._rag_options(chunk_size, chunk_overlap)
        cs = opts["chunk_size"]
        co = opts["chunk_overlap"]

        from sqlalchemy import select

        # Phase 1: mark as processing, capture file info into a plain dict.
        async with session_scope() as session:
            doc = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            if doc is None:
                raise DocumentError(f"Document not found: {document_id}")
            if doc.status == "ready":
                if on_started is not None:
                    on_started.set()
                return doc.to_dict()

            # Guard against two workers ingesting the same document (the admin
            # UI's button plus the retry script, say): they'd interleave slices
            # and overwrite each other's progress counter. A row untouched for
            # STALE_PROCESSING_MINUTES is treated as abandoned and reclaimed.
            if doc.status == "processing":
                from datetime import timedelta

                from ..utils.timeutil import utcnow_naive

                fresh_cutoff = utcnow_naive() - timedelta(
                    minutes=self.STALE_PROCESSING_MINUTES
                )
                if doc.updated_at and doc.updated_at > fresh_cutoff:
                    raise DocumentError(
                        f"该文档正在入库中（已完成 {doc.chunks_done}/{doc.chunks_total} "
                        f"个片段），请等它结束或稍后重试。"
                    )
                log.info("Reclaiming abandoned ingest of %s", doc.filename)

            # Resume a previous partial run, but only if the chunking settings
            # are unchanged — different chunk_size means different chunk ids,
            # so the old vectors don't correspond to the new slices.
            resume_from = 0
            same_settings = doc.chunk_size == cs and doc.chunk_overlap == co
            if doc.chunks_done and same_settings:
                resume_from = doc.chunks_done
                log.info(
                    "Resuming %s from chunk %d (previous run stopped there)",
                    doc.filename, resume_from,
                )
            elif doc.chunks_done and not same_settings:
                log.info(
                    "Chunk settings changed (%s/%s → %s/%s); re-indexing %s from scratch",
                    doc.chunk_size, doc.chunk_overlap, cs, co, doc.filename,
                )

            doc.status = "processing"
            doc.chunk_size = cs
            doc.chunk_overlap = co
            doc.error_message = None
            payload = {
                "file_path": doc.file_path,
                "filename": doc.filename,
                "parser_code": doc.parser_code or "txt",
            }

        # The row is now `processing` — the queued API response can report a
        # truthful status while ingestion continues in the background.
        if on_started is not None:
            on_started.set()

        # Phase 2: parse + chunk + embed + index (no DB access).
        new_status = "ready"
        err_msg: Optional[str] = None
        count = 0
        try:
            from ..parsers import get_parser_by_code

            parser = get_parser_by_code(payload["parser_code"])
            # Parsing (PDF especially) and chunking are CPU/blocking-disk work
            # that can take seconds — off the event loop, or every concurrent
            # chat stream freezes while a document ingests.
            text = await asyncio.to_thread(parser.parse, payload["file_path"])
            if not text or not text.strip():
                raise DocumentError("Parsed text is empty")

            chunks: List[TextChunk] = await asyncio.to_thread(
                lambda: split_text(
                    text,
                    chunk_size=cs,
                    chunk_overlap=co,
                    strategy=opts["strategy"],
                    prefix_heading=opts["prefix_heading"],
                )
            )
            if not chunks:
                raise DocumentError("No chunks produced")

            records: List[ChunkRecord] = []
            for ch in chunks:
                cid = f"{document_id}:{ch.index}"
                meta: Dict[str, Any] = {
                    "filename": payload["filename"],
                    "document_id": document_id,
                    "chunk_index": ch.index,
                }
                # Keep the heading so answers can cite "which section".
                if ch.heading:
                    meta["heading"] = ch.heading
                records.append(ChunkRecord(chunk_id=cid, text=ch.text, metadata=meta))

            await self._set_progress(document_id, done=resume_from, total=len(records))

            # Embed and index in slices, resuming where a previous attempt
            # stopped. Vendor embedding quotas (ARK's especially) can run out
            # part-way through a large knowledge base; without resume the user
            # would re-pay for the same chunks on every retry and might never
            # finish. `add()` upserts by chunk_id, so re-running a slice is safe.
            embedder = self.embedder()
            store = await self.open_vector_store()
            done = resume_from
            actual_dim = 0
            partial_error: Optional[str] = None
            for start in range(resume_from, len(records), self.INDEX_SLICE):
                sl = records[start : start + self.INDEX_SLICE]
                try:
                    vectors = await embedder.embed([r.text for r in sl])
                    if vectors and vectors[0]:
                        actual_dim = len(vectors[0])
                except Exception as e:
                    # Keep what's already indexed and report how far we got —
                    # far more useful than discarding an hour of work.
                    partial_error = str(e)
                    # No stack trace: hitting a vendor quota mid-ingest is an
                    # expected, recoverable outcome, and the retry script logs
                    # one of these per round.
                    log.warning(
                        "Embedding stopped at chunk %d/%d (will resume on retry)",
                        done, len(records),
                    )
                    break
                await store.add(document_id, sl, vectors)
                # Count what we've *walked past*, not what add() returned —
                # this is the resume offset, so it must track position in
                # `records`, and re-running a slice must not double-count.
                done = start + len(sl)
                await self._set_progress(document_id, done=done, total=len(records))

            count = done
            if partial_error:
                raise DocumentError(
                    f"已完成 {done}/{len(records)} 个片段后中断。"
                    f"再点一次「入库」会从第 {done} 个片段继续，已完成的不会重复消耗配额。"
                    f"原因：{partial_error[:400]}"
                )
        except DocumentError as e:
            # Expected, actionable failures (quota exhausted mid-ingest,
            # unsupported file, empty parse). The message already explains what
            # to do, so a stack trace is just noise — and the retry script
            # prints one per round.
            log.warning("process_document incomplete: %s", str(e)[:200])
            new_status = "failed"
            err_msg = str(e)[:2000]
        except Exception as e:
            # Unexpected — keep the traceback, it's a bug worth seeing.
            log.exception("process_document failed: %s", e)
            new_status = "failed"
            err_msg = str(e)[:2000]

        # Phase 3: write the final status back.
        # Default covers the rare case where the row was deleted mid-ingest:
        # the fresh fetch below finds nothing, and the caller still gets a dict.
        result: Dict[str, Any] = {
            "id": document_id, "status": new_status, "chunk_count": count,
        }
        async with session_scope() as session:
            doc = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            if doc is not None:
                doc.status = new_status
                doc.error_message = err_msg
                if new_status == "ready":
                    doc.chunk_count = count
                    doc.chunks_done = count
                    doc.chunks_total = count
                else:
                    # Keep whatever was indexed so a retry can resume from
                    # there instead of re-embedding (and re-paying for) it.
                    doc.chunk_count = doc.chunks_done or 0
                await session.flush()
                await session.refresh(doc)  # reload server-set columns (updated_at)
                result = doc.to_dict()

        # An index built before the signature feature existed has no stamp;
        # record the current model after a fresh full ingest so the console
        # can later detect a model swap. Never overwrite an existing stamp —
        # only the rebuild pipeline may change it (lazy import: reindex_service
        # imports this module).
        if new_status == "ready" and actual_dim and resume_from == 0:
            await self._maybe_stamp_index_signature(actual_dim)
        return result

    async def list_documents(self) -> List[Dict[str, Any]]:
        """List documents, flipping abandoned `processing` rows to `failed`.

        If the worker died mid-ingest (restart, kill, crash) the row keeps
        saying "处理中" forever, and the UI spins on a job nobody is running.
        Anything untouched for STALE_PROCESSING_MINUTES gets marked failed with
        an actionable message so the user can retry.
        """
        from datetime import timedelta

        from sqlalchemy import select

        from ..utils.timeutil import utcnow_naive

        cutoff = utcnow_naive() - timedelta(minutes=self.STALE_PROCESSING_MINUTES)
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Document).order_by(Document.created_at.desc())
                )
            ).scalars().all()

            for r in rows:
                if r.status == "processing" and r.updated_at and r.updated_at < cutoff:
                    done, total = r.chunks_done, r.chunks_total
                    r.status = "failed"
                    r.error_message = (
                        f"入库中断（服务重启或进程退出），已完成 {done}/{total} 个片段。"
                        "请点「入库」重试。"
                    )
                    log.warning("Marking stale processing document as failed: %s", r.filename)
            return [r.to_dict() for r in rows]

    async def delete_document(self, document_id: str) -> bool:
        from sqlalchemy import select
        async with session_scope() as session:
            doc = (
                await session.execute(select(Document).where(Document.id == document_id))
            ).scalar_one_or_none()
            if doc is None:
                return False
            file_path = doc.file_path
            await session.delete(doc)
            await session.flush()
        try:
            store = await self.open_vector_store()
            await store.delete_document(document_id)
        except Exception as e:
            log.warning("vector delete failed for %s: %s", document_id, e)
        try:
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass
        return True

    # ---- CLI helper -------------------------------------------------
    async def ingest_path(self, path: Path) -> Dict[str, Any]:
        path = Path(path)
        data = await self.save_upload(path.name, path.read_bytes(), mime_type=None)
        if data.get("status") == "ready":
            return data
        return await self.process_document(data["id"])
