"""Chat API: non-streaming /chat and streaming /chat/stream (SSE)."""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import LLMMessage
from ..core.config import get_settings
from ..core.database import get_db
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
from ..utils.sse import sse_pack

log = logging.getLogger(__name__)
router = APIRouter()


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


# ----- Endpoints -------------------------------------------------------
@router.post("", response_model=ChatOut, summary="One-shot RAG chat (JSON)")
async def chat(body: ChatIn, db: AsyncSession = Depends(get_db)) -> ChatOut:
    settings = get_settings()
    session_svc = SessionService(db)
    lang = normalize_lang(body.lang)

    session_id = body.session_id
    if not session_id:
        sess = await session_svc.create_session(
            title=body.message[:30] or pick(NEW_SESSION_TITLE, lang),
            meta={"lang": lang},
        )
        session_id = sess.id

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

    async def gen() -> AsyncIterator[bytes]:
        session_id = body.session_id
        if not session_id:
            sess = await session_svc.create_session(
                title=body.message[:30] or pick(NEW_SESSION_TITLE, lang),
                meta={"lang": lang},
            )
            session_id = sess.id

        yield _sse("meta", {"session_id": session_id, "lang": lang})

        history_llm: List[LLMMessage] = []
        if body.history:
            history_llm = _history_to_llm(body.history)
        else:
            history_llm = await session_svc.build_history(session_id)
        # Persist after the history is read — see the non-streaming endpoint.
        await session_svc.add_message(session_id, "user", body.message)

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
            await rag.aclose()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        },
    )
