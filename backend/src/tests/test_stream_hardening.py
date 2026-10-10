"""Regression tests for the streaming-hardening batch (M7 + M9 + F2).

Covers:
  * with_heartbeat — keep-alive comments during stalls, error passthrough,
    and consumer-disconnect cancellation propagated INTO the inner generator.
  * Partial-answer retention — a client that vanishes mid-answer keeps the
    streamed text in the DB, flagged ``meta.partial``, and the user turn
    survives the disconnect too (committed before the stream starts).
  * Adapter stream diagnostics — an Anthropic mid-stream ``error`` event and
    a body of unparseable data lines (HTML from a mis-pointed gateway) now
    raise LLMProtocolError instead of silently ending the stream.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from src.api import chat as chat_api
from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.models.base import Base
from src.models.message import Message


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
async def db_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/stream.db")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    await dispose_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()
    get_settings.cache_clear()


def _sse_client(body: bytes) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _cfg():
    from src.adapters.base import LLMConfig

    return LLMConfig(api_key="k", base_url="https://api.test", model="m")


# ---------------------------------------------------------------------------
# M7 — heartbeat wrapper
# ---------------------------------------------------------------------------
async def test_heartbeat_emits_keepalive_during_stall() -> None:
    async def slow():
        yield b"first"
        await asyncio.sleep(0.25)   # model "thinking"
        yield b"second"

    out = []
    async for chunk in chat_api.with_heartbeat(slow(), interval=0.05):
        out.append(chunk)
    assert out[0] == b"first"
    assert out[-1] == b"second"
    keepers = out.count(b": keep-alive\n\n")
    assert keepers >= 2, "a stalled stream must produce keep-alive comments"


async def test_heartbeat_passes_items_and_propagates_inner_error() -> None:
    async def bad():
        yield b"x"
        raise RuntimeError("boom")

    out = []
    with pytest.raises(RuntimeError, match="boom"):
        async for chunk in chat_api.with_heartbeat(bad(), interval=5):
            out.append(chunk)
    assert out == [b"x"]


async def test_heartbeat_consumer_close_cancels_inner() -> None:
    """The disconnect signal must reach the inner generator as CancelledError
    — that is the hook chat's gen() uses to persist the partial answer."""
    cancelled = asyncio.Event()

    async def inner():
        try:
            yield b"a"
            await asyncio.sleep(30)
            yield b"never"
        except asyncio.CancelledError:
            cancelled.set()
            raise

    it = chat_api.with_heartbeat(inner(), interval=5)
    first = await asyncio.wait_for(it.__anext__(), timeout=5)
    assert first == b"a"
    await it.aclose()               # consumer went away
    assert cancelled.is_set()


# ---------------------------------------------------------------------------
# M9 — partial answer retention
# ---------------------------------------------------------------------------
async def test_persist_partial_saves_flagged_row(db_env) -> None:
    from src.services.session_service import SessionService

    async with session_scope() as db:
        sess = await SessionService(db).create_session(title="t")
        sid = sess.id

    await chat_api._persist_partial(sid, "半截回答", [], "zh-CN")

    async with session_scope() as db:
        msgs = (
            await db.execute(select(Message).where(Message.session_id == sid))
        ).scalars().all()
    assert len(msgs) == 1
    m = msgs[0]
    assert m.role == "assistant"
    assert m.content == "半截回答"
    assert (m.meta or {}).get("partial") is True
    assert (m.meta or {}).get("lang") == "zh-CN"


async def test_client_disconnect_persists_partial_answer(db_env, monkeypatch) -> None:
    class StallRAG:
        """Yields two tokens, then hangs forever — the client vanishes here."""

        def __init__(self, settings=None, **kw):
            pass

        async def stream_ask(self, question, **kw):
            yield "你好", None
            yield "，世界", None
            await asyncio.sleep(30)
            yield "never", []

        async def aclose(self):
            pass

    monkeypatch.setattr(chat_api, "RAGService", StallRAG)

    body = chat_api.ChatIn(message="你好吗", lang="zh-CN")
    async with session_scope() as db:
        resp = await chat_api.chat_stream(body, db)
        it = resp.body_iterator
        tokens = 0
        while tokens < 2:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=5)
            if b'"text"' in chunk:
                tokens += 1
        await it.aclose()           # simulate the client disconnecting
    await asyncio.sleep(0.05)

    async with session_scope() as db2:
        rows = (await db2.execute(select(Message))).scalars().all()

    by_role: dict[str, list] = {}
    for m in rows:
        by_role.setdefault(m.role, []).append(m)
    assert len(by_role.get("user", [])) == 1, "the question must survive the disconnect"
    partial = by_role.get("assistant", [])
    assert len(partial) == 1, "the streamed prefix must be persisted, not lost"
    assert partial[0].content == "你好，世界"
    assert (partial[0].meta or {}).get("partial") is True


# ---------------------------------------------------------------------------
# F2 — adapter stream diagnostics
# ---------------------------------------------------------------------------
async def test_anthropic_stream_error_event_raises() -> None:
    from src.adapters.anthropic_adapter import AnthropicStyleProvider
    from src.adapters.base import LLMMessage, LLMProtocolError

    body = (
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    p = AnthropicStyleProvider(_cfg(), client=_sse_client(body))
    got = []
    with pytest.raises(LLMProtocolError) as ei:
        async for piece in p.stream_chat([LLMMessage(role="user", content="q")]):
            got.append(piece)
    assert got == ["hi"], "tokens before the error event still reach the caller"
    assert "overloaded_error" in str(ei.value)
    await p.aclose()


async def test_anthropic_stream_unparseable_body_raises() -> None:
    from src.adapters.anthropic_adapter import AnthropicStyleProvider
    from src.adapters.base import LLMMessage, LLMProtocolError

    # A gateway that answers 200 with HTML (wrong base URL / proxy error page).
    body = b"data: <html>\ndata: <p>502 Bad Gateway</p>\n\n"
    p = AnthropicStyleProvider(_cfg(), client=_sse_client(body))
    with pytest.raises(LLMProtocolError, match="没有可解析"):
        async for _ in p.stream_chat([LLMMessage(role="user", content="q")]):
            pass
    await p.aclose()


async def test_openai_stream_unparseable_body_raises() -> None:
    from src.adapters.base import LLMMessage, LLMProtocolError
    from src.adapters.openai_adapter import OpenAIStyleProvider

    body = b"data: <html>\ndata: <p>502 Bad Gateway</p>\n\n"
    p = OpenAIStyleProvider(_cfg(), client=_sse_client(body))
    with pytest.raises(LLMProtocolError, match="没有可解析"):
        async for _ in p.stream_chat([LLMMessage(role="user", content="q")]):
            pass
    await p.aclose()


async def test_openai_stream_done_only_is_not_an_error() -> None:
    """`[DONE]` alone is a legitimate empty completion — no false alarm."""
    from src.adapters.base import LLMMessage
    from src.adapters.openai_adapter import OpenAIStyleProvider

    p = OpenAIStyleProvider(_cfg(), client=_sse_client(b"data: [DONE]\n\n"))
    got = []
    async for piece in p.stream_chat([LLMMessage(role="user", content="q")]):
        got.append(piece)
    assert got == []
    await p.aclose()


async def test_openai_stream_normal_path_unchanged() -> None:
    from src.adapters.base import LLMMessage
    from src.adapters.openai_adapter import OpenAIStyleProvider

    body = (
        b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    p = OpenAIStyleProvider(_cfg(), client=_sse_client(body))
    got = []
    async for piece in p.stream_chat([LLMMessage(role="user", content="q")]):
        got.append(piece)
    assert got == ["你好", " world"]
    await p.aclose()
