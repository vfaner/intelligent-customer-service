"""Session + message persistence service."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters.base import LLMMessage
from ..models.message import Message
from ..models.session import Session

# Caps shared by server-side history building and the client-supplied history
# sanitizer in api/chat.py — a client override must not be able to exceed what
# the server itself would feed the model.
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 6000


def _new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    """Naive UTC now (datetime.utcnow() is deprecated since 3.12).

    Naive on purpose: it matches what SQLite's func.now() stores and what the
    existing rows already contain, so ordering stays consistent.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SessionService:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ----- session CRUD ----------------------------------------------
    async def create_session(
        self,
        *,
        user_id: Optional[str] = None,
        title: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Session:
        sess = Session(
            id=_new_id(),
            user_id=user_id,
            title=title or "新会话",
            status="active",
            meta=meta or {},
        )
        self.s.add(sess)
        await self.s.flush()
        return sess

    async def get_session(self, session_id: str) -> Optional[Session]:
        res = await self.s.execute(
            select(Session).where(Session.id == session_id)
        )
        return res.scalar_one_or_none()

    async def list_sessions(
        self, *, user_id: Optional[str] = None, limit: int = 50
    ) -> List[Session]:
        stmt = select(Session).order_by(Session.updated_at.desc()).limit(limit)
        if user_id:
            stmt = stmt.where(Session.user_id == user_id)
        res = await self.s.execute(stmt)
        return list(res.scalars().all())

    async def close_session(self, session_id: str) -> bool:
        sess = await self.get_session(session_id)
        if sess is None:
            return False
        sess.status = "closed"
        await self.s.flush()
        return True

    # ----- messages ---------------------------------------------------
    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        sources: Optional[List[Dict[str, Any]]] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Message:
        msg = Message(
            id=_new_id(),
            session_id=session_id,
            role=role,
            content=content,
            sources=sources,
            meta=meta or {},
            # Explicit Python-side timestamp: SQLite's func.now() default has
            # only second precision, which made created_at ordering ambiguous
            # for a user+assistant pair written within the same second.
            created_at=_utcnow(),
        )
        self.s.add(msg)
        # Touch session.updated_at
        sess = await self.get_session(session_id)
        if sess is not None:
            sess.updated_at = _utcnow()
            if not sess.title or sess.title == "新会话":
                # Auto-title from first user message
                if role == "user" and content:
                    sess.title = content[:30]
        await self.s.flush()
        return msg

    async def get_messages(self, session_id: str, *, limit: int = 200) -> List[Message]:
        """Return the most recent ``limit`` messages, oldest first.

        The previous ``asc().limit()`` returned the OLDEST window instead, so
        any session longer than the limit showed — and fed the model — nothing
        but its opening turns.
        """
        sess = await self.get_session(session_id)
        if sess is None:
            return []
        res = await self.s.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return list(reversed(res.scalars().all()))

    # ----- helpers for RAG multi-turn context -------------------------
    async def build_history(
        self,
        session_id: str,
        *,
        max_messages: int = MAX_HISTORY_MESSAGES,
        max_chars: int = MAX_HISTORY_CHARS,
    ) -> List[LLMMessage]:
        """Return a trimmed history of ``[user, assistant, ...]`` for context.

        Truncation is a simple head-cut on character count so the most
        recent turns (which are most relevant) survive.
        """
        msgs = await self.get_messages(session_id, limit=max_messages)
        if not msgs:
            return []
        out: List[LLMMessage] = []
        used = 0
        # Walk from the most recent backwards
        for m in reversed(msgs):
            if m.role not in ("user", "assistant"):
                continue
            piece = (m.content or "").strip()
            if not piece:
                continue
            if used + len(piece) > max_chars:
                break
            out.append(LLMMessage(role=m.role, content=piece))
            used += len(piece)
        out.reverse()
        return out
