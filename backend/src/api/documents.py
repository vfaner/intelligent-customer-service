"""Document upload / process / list / delete API."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel

from ..core.config import get_settings
from ..core.exceptions import DocumentError, NotFoundError
from ..core.registry import list_document_parsers
from ..services.document_service import DocumentService

log = logging.getLogger(__name__)
router = APIRouter()

# In-flight background ingestions: document_id -> task. Guards double-submit
# (double-click, two admins) the same way the reindex job guard does.
_ingest_tasks: Dict[str, asyncio.Task] = {}


# ----- Schemas ---------------------------------------------------------
class ProcessIn(BaseModel):
    document_id: str
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None


class DocumentOut(BaseModel):
    id: str
    filename: str
    file_size: int
    file_md5: str
    mime_type: Optional[str] = None
    parser_code: Optional[str] = None
    status: str
    chunk_count: int
    chunks_done: int = 0
    chunks_total: int = 0
    error_message: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class UploadOut(DocumentOut):
    pass


# ----- Endpoints -------------------------------------------------------
@router.post("/upload", response_model=UploadOut, summary="Upload a knowledge document")
async def upload_document(file: UploadFile = File(...)) -> UploadOut:
    settings = get_settings()
    max_bytes = settings.app.max_upload_mb * 1024 * 1024
    # Read in bounded chunks and abort the moment the limit is crossed. The
    # old `await file.read()` buffered the ENTIRE body before checking its
    # size, so one oversized upload could exhaust server memory before being
    # rejected — the check only protects the disk, not the process.
    chunks: List[bytes] = []
    total = 0
    while True:
        piece = await file.read(1024 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise DocumentError(
                f"File too large: over {max_bytes} bytes (limit {max_bytes})",
                details={"limit_mb": settings.app.max_upload_mb},
            )
        chunks.append(piece)
    content = b"".join(chunks)
    if not content:
        raise DocumentError("Empty file")
    svc = DocumentService(settings)
    info = await svc.save_upload(file.filename or "unnamed", content, mime_type=file.content_type)
    return UploadOut(**info)


async def _run_ingest(
    svc: DocumentService,
    doc_id: str,
    chunk_size: Optional[int],
    chunk_overlap: Optional[int],
    started: asyncio.Event,
) -> None:
    try:
        await svc.process_document(
            doc_id,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            on_started=started,
        )
    except Exception as e:
        # process_document persists `failed` + the reason for every expected
        # error; this last-resort net keeps an unexpected crash from dying
        # silently inside a detached task.
        log.warning("background ingest of %s raised: %s", doc_id, str(e)[:300])
    finally:
        started.set()
        if _ingest_tasks.get(doc_id) is asyncio.current_task():
            _ingest_tasks.pop(doc_id, None)


@router.post(
    "/process",
    status_code=202,
    response_model=DocumentOut,
    summary="Queue chunk + embed + index for a document (runs in background)",
)
async def process_document(body: ProcessIn) -> DocumentOut:
    """Start ingestion in the background and return 202 right away.

    A large document takes minutes (parsing, embedding-vendor quota); holding
    the HTTP request open that long looked like a hang and died on proxy
    timeouts. The response carries the current row — normally
    ``status="processing"`` — and the console polls ``/list`` for
    ``chunks_done``/``chunks_total`` progress.
    """
    settings = get_settings()
    svc = DocumentService(settings)
    doc_id = body.document_id

    running = _ingest_tasks.get(doc_id)
    if running is not None and not running.done():
        raise DocumentError("该文档正在后台入库中，请在列表查看进度")

    started = asyncio.Event()
    task = asyncio.create_task(
        _run_ingest(svc, doc_id, body.chunk_size, body.chunk_overlap, started)
    )
    _ingest_tasks[doc_id] = task
    try:
        # Wait until the row has flipped to `processing` (or the call returned
        # early) so the 202 body reports a truthful status. Bounded: if phase 1
        # is somehow slow, the client still gets the queued row.
        await asyncio.wait_for(started.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("ingest of %s had not started after 5s; returning row as-is", doc_id)

    info = await svc.fetch_document(doc_id)
    if info is None:
        raise NotFoundError(f"Document not found: {doc_id}")
    return DocumentOut(**info)


@router.get("/list", response_model=List[DocumentOut], summary="List indexed documents")
async def list_documents() -> List[DocumentOut]:
    settings = get_settings()
    svc = DocumentService(settings)
    rows = await svc.list_documents()
    return [DocumentOut(**r) for r in rows]


@router.delete("/{document_id}", summary="Delete a document and its vectors")
async def delete_document(document_id: str) -> dict:
    settings = get_settings()
    svc = DocumentService(settings)
    ok = await svc.delete_document(document_id)
    if not ok:
        raise NotFoundError(f"Document not found: {document_id}")
    return {"ok": True}


@router.get("/parsers", summary="List supported document parsers")
async def parsers() -> dict:
    return {"parsers": list_document_parsers()}


# ----- Full-index rebuild (embedding model changed) --------------------
@router.post("/reindex", summary="Rebuild the whole vector index with the current embedding model")
async def start_reindex() -> dict:
    """Start a background rebuild. Safe to call repeatedly: while a rebuild
    is running the request is rejected with 409; a later call after a partial
    failure resumes unfinished documents without dropping completed vectors.
    """
    from fastapi.responses import JSONResponse

    from ..services.reindex_service import ReindexInProgress, start_reindex as _start

    try:
        return await _start(get_settings())
    except ReindexInProgress as e:
        return JSONResponse(status_code=409, content={"detail": str(e)})


@router.get("/reindex/status", summary="Rebuild job status + model-signature mismatch")
async def reindex_status() -> dict:
    from ..services.reindex_service import status_payload

    return await status_payload(get_settings())


class SplitPreviewIn(BaseModel):
    text: Optional[str] = None
    document_id: Optional[str] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    strategy: Optional[str] = None            # structure | window
    prefix_heading: Optional[bool] = None


@router.post("/split-preview", summary="Preview how a document would be chunked")
async def split_preview(body: SplitPreviewIn) -> dict:
    """Chunk text without embedding or indexing it.

    Lets the admin tune chunk size / strategy and see the result immediately.
    Either paste ``text`` directly or pass a ``document_id`` that's already
    been uploaded.
    """
    from ..parsers import get_parser_by_code
    from ..utils.text_splitter import split_text

    settings = get_settings()
    svc = DocumentService(settings)
    opts = await svc._rag_options(body.chunk_size, body.chunk_overlap)
    strategy = body.strategy or opts["strategy"]
    prefix = opts["prefix_heading"] if body.prefix_heading is None else body.prefix_heading

    text = body.text or ""
    source = "粘贴的文本"
    if not text and body.document_id:
        from sqlalchemy import select

        from ..core.database import session_scope
        from ..models.document import Document

        async with session_scope() as session:
            doc = (
                await session.execute(select(Document).where(Document.id == body.document_id))
            ).scalar_one_or_none()
            if doc is None:
                raise NotFoundError(f"Document not found: {body.document_id}")
            file_path, parser_code, filename = doc.file_path, doc.parser_code or "txt", doc.filename
        # Interactive endpoint, but parsing a big PDF still blocks for seconds
        # — run it off the event loop like the ingest path does.
        text = await asyncio.to_thread(get_parser_by_code(parser_code).parse, file_path)
        source = filename

    if not text.strip():
        raise DocumentError("没有可切分的内容：请粘贴文本或选择一个已上传的文档")

    chunks = await asyncio.to_thread(
        lambda: split_text(
            text,
            chunk_size=opts["chunk_size"],
            chunk_overlap=opts["chunk_overlap"],
            strategy=strategy,
            prefix_heading=prefix,
        )
    )
    sizes = [len(c.text) for c in chunks] or [0]
    return {
        "source": source,
        "strategy": strategy,
        "chunk_size": opts["chunk_size"],
        "chunk_overlap": opts["chunk_overlap"],
        "prefix_heading": prefix,
        "total_chars": len(text),
        "chunk_count": len(chunks),
        "avg_chars": round(sum(sizes) / len(sizes)),
        "min_chars": min(sizes),
        "max_chars": max(sizes),
        "chunks": [
            {
                "index": c.index,
                "heading": c.heading,
                "chars": len(c.text),
                "text": c.text,
            }
            for c in chunks[:60]      # cap the payload; enough to judge quality
        ],
        "truncated": len(chunks) > 60,
    }
