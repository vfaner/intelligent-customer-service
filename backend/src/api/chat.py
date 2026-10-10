"""Chat API: non-streaming /chat and streaming /chat/stream (SSE)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import LLMMessage
from ..core.config import get_settings
from ..core.database import get_db, session_scope
from ..core.exceptions import NotFoundError, ValidationError
from ..core.i18n import (
    DEFAULT_LANG,
    ERROR_PREFIX,
    NEW_SESSION_TITLE,
    normalize_lang,
    pick,
)
from ..services.rag_service import RAGService
from ..services.session_service import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    SessionService,
)
from ..utils.sse import make_keeper, sse_pack

log = logging.getLogger(__name__)
router = APIRouter()

# Send an SSE comment when the stream stalls longer than this. A slow model
# "thinking" looks exactly like a dead connection to nginx (60s default),
# corporate proxies, and browsers; the comment keeps them from cutting us off.
STREAM_HEARTBEAT_SECONDS = 15.0


async def with_heartbeat(
    inner: AsyncIterator[bytes], interval: float = STREAM_HEARTBEAT_SECONDS
) -> AsyncIterator[bytes]:
    """Wrap an SSE byte iterator, emitting keep-alive comments during stalls.

    ``inner`` is pumped in a separate task rather than awaited through
    ``asyncio.wait_for`` directly: cancelling a timed-out ``__anext__`` would
    throw into the generator and corrupt it. When the consumer goes away
    (client disconnect), the pump task is cancelled instead, which surfaces
    inside ``inner`` as ``CancelledError`` — the chat generator uses exactly
    that hook to persist the partial answer.
    """
    sentinel = object()
    q: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in inner:
                await q.put(item)
        except BaseException as e:  # hand the original to the consumer
            await q.put(e)
        else:
            await q.put(sentinel)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield make_keeper()
                continue
            if item is sentinel:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass  # the cancellation we just caused (or inner's own error)


# ----- Schemas ---------------------------------------------------------
class ChatIn(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(..., min_length=1)
    agent_name: Optional[str] = None
    history: Optional[List[dict]] = None  # optional override; else loaded from session
    # UI language resolved by the client (see frontend/shared/locale.js). Drives
    # the answer's language and the canned refusal. Unknown/absent values fall
    # back to zh-CN, so clients predating this field behave exactly as before.
    lang: Optional[str] = None


class ChatOut(BaseModel):
    session_id: str
    answer: str
    sources: list = Field(default_factory=list)
    lang: str = DEFAULT_LANG


# ----- Helpers ---------------------------------------------------------
def _sse(event: str, data, *, id: Optional[str] = None) -> bytes:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, default=str)
    return sse_pack(event, data, id=id)


def _history_to_llm(history: List[dict]) -> List[LLMMessage]:
    """Sanitize a client-supplied history override.

    Only user/assistant roles are accepted: a client-provided ``system``
    message would sit right after the RAG system prompt and could override
    the "answer only from the retrieved documents" constraint. The same caps
    as the server-side history apply, so the public endpoint cannot be used
    to burn unlimited tokens.
    """
    out: List[LLMMessage] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            piece = content.strip()
            if piece:
                out.append(LLMMessage(role=role, content=piece))
    out = out[-MAX_HISTORY_MESSAGES:]
    # Head-cut on character count, keeping the most recent turns — mirrors
    # SessionService.build_history.
    trimmed: List[LLMMessage] = []
    used = 0
    for m in reversed(out):
        if used + len(m.content) > MAX_HISTORY_CHARS:
            break
        trimmed.append(m)
        used += len(m.content)
    trimmed.reverse()
    return trimmed


async def _persist_partial(
    session_id: str, text: str, sources: list, lang: str
) -> None:
    """Save an interrupted-but-partial answer using its OWN DB session.

    Runs while the request is being cancelled, so the request-scoped session
    is already being torn down and unusable here.
    """
    async with session_scope() as db:
        svc = SessionService(db)
        await svc.add_message(
            session_id,
            "assistant",
            text,
            sources=sources,
            meta={"partial": True, "lang": lang},
        )


async def _resolve_session(svc: SessionService, body: ChatIn, lang: str) -> str:
    """Return a usable session id, validating a client-supplied one.

    An unknown id used to be accepted blindly — every message landed in an
    orphan thread nobody could read back. A session an admin has closed must
    stop accepting turns.
    """
    if body.session_id:
        sess = await svc.get_session(body.session_id)
        if sess is None:
            raise NotFoundError(f"Session not found: {body.session_id}")
        if sess.status == "closed":
            raise ValidationError("会话已关闭，请开始新会话")
        return body.session_id
    sess = await svc.create_session(
        title=body.message[:30] or pick(NEW_SESSION_TITLE, lang),
        meta={"lang": lang},
    )
    return sess.id


# ----- Endpoints -------------------------------------------------------
@router.post("", response_model=ChatOut, summary="One-shot RAG chat (JSON)")
async def chat(body: ChatIn, db: AsyncSession = Depends(get_db)) -> ChatOut:
    settings = get_settings()
    session_svc = SessionService(db)
    lang = normalize_lang(body.lang)

    session_id = await _resolve_session(session_svc, body, lang)

    history_llm: List[LLMMessage] = []
    if body.history:
        history_llm = _history_to_llm(body.history)
    else:
        history_llm = await session_svc.build_history(session_id)

    # Persist the user turn *after* the history is read: add_message flushes,
    # and build_history in the same transaction would then return the current
    # question as the last history item — RAGService appends the question
    # itself, so the model saw it twice.
    await session_svc.add_message(session_id, "user", body.message)

    rag = RAGService(settings)
    try:
        answer, sources = await rag.ask(
            body.message,
            history=history_llm,
            agent_name=body.agent_name,
            lang=lang,
        )
    finally:
        await rag.aclose()

    await session_svc.add_message(session_id, "assistant", answer, sources=sources)
    return ChatOut(session_id=session_id, answer=answer, sources=sources, lang=lang)


@router.post("/stream", summary="Streaming RAG chat via SSE")
async def chat_stream(body: ChatIn, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    """Server-Sent Events stream. Events:

      - ``meta``   : first, carries ``{session_id}``
      - ``token``  : incremental text from the LLM
      - ``sources``: final event, carries the matched chunks
      - ``done``   : end of stream
      - ``error``  : on failure, carries the error message
    """
    settings = get_settings()
    session_svc = SessionService(db)
    lang = normalize_lang(body.lang)
    # Resolve (and validate) the session BEFORE the stream starts, so an
    # unknown/closed id surfaces as a normal JSON 404/422 instead of an SSE
    # error event halfway through.
    session_id = await _resolve_session(session_svc, body, lang)

    async def gen() -> AsyncIterator[bytes]:
        yield _sse("meta", {"session_id": session_id, "lang": lang})

        history_llm: List[LLMMessage] = []
        if body.history:
            history_llm = _history_to_llm(body.history)
        else:
            history_llm = await session_svc.build_history(session_id)
        # Persist after the history is read — see the non-streaming endpoint.
        await session_svc.add_message(session_id, "user", body.message)
        # Commit the user turn (and a freshly created session row) right away:
        # (1) a client disconnect cancels this request, and get_db would roll
        #     the uncommitted turn back — the question, and the session the
        #     partial answer below references, must survive the stream;
        # (2) on SQLite the write transaction would otherwise be held for the
        #     entire stream, blocking every other writer (ingest progress,
        #     admin actions) for its whole duration.
        await db.commit()

        rag = RAGService(settings)
        answer_parts: List[str] = []
        sources_payload: Optional[list] = None
        try:
            async for token, sources in rag.stream_ask(
                body.message,
                history=history_llm,
                agent_name=body.agent_name,
                lang=lang,
            ):
                # Two independent checks, not if/elif: the canned-refusal event
                # carries BOTH text and an empty sources list in one tuple.
                # The old `if sources is not None / elif token` swallowed the
                # refusal text — the client got an empty bubble and the DB
                # stored an empty answer.
                if token:
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
                if sources is not None:
                    sources_payload = sources

            full_answer = "".join(answer_parts)
            if sources_payload is None:
                # stream_ask guarantees a final event; safety net
                sources_payload = []
            yield _sse("sources", sources_payload)
            yield _sse("done", {"ok": True})
            await session_svc.add_message(session_id, "assistant", full_answer, sources=sources_payload)
        except asyncio.CancelledError:
            # The client vanished mid-answer (closed tab, dropped network,
            # proxy timeout) — with_heartbeat's pump cancels this generator.
            # Whatever was streamed is still a real (and paid-for) answer:
            # persist it flagged partial instead of losing the turn, then let
            # the cancellation propagate. Shielded because we're already in a
            # cancelled context; fresh session because the request's is dying.
            partial = "".join(answer_parts).strip()
            if partial:
                try:
                    await asyncio.shield(
                        _persist_partial(session_id, partial, sources_payload or [], lang)
                    )
                except Exception:
                    log.warning(
                        "could not persist partial answer for session %s",
                        session_id, exc_info=True,
                    )
            raise
        except Exception as e:
            log.exception("chat stream failed: %s", e)
            yield _sse("error", {"message": str(e)})
            yield _sse("done", {"ok": False})
            try:
                await session_svc.add_message(
                    session_id, "assistant", f"{pick(ERROR_PREFIX, lang)} {e}", meta={"error": True}
                )
            except Exception:
                pass
        finally:
            # Pure in-memory cleanup now that the embedder/store are shared
            # singletons (services.runtime) — safe even while cancelled.
            await rag.aclose()

    return StreamingResponse(
        with_heartbeat(gen()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
