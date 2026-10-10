"""Session management API."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.database import get_db
from ..core.exceptions import NotFoundError
from ..models.session import Session as ChatSession
from ..services.session_service import SessionService

# Public: session creation (paired with /api/chat) and reading back the
# messages of a session id the caller already holds — a capability URL, which
# is what lets an embedded widget restore its own history.
router = APIRouter()

# Admin-only: anything that enumerates or mutates OTHER people's sessions.
# Listing sessions and reading arbitrary message threads used to be public,
# which let any anonymous visitor dump every customer conversation.
admin_router = APIRouter()


# ----- Schemas ---------------------------------------------------------
class SessionCreateIn(BaseModel):
    user_id: Optional[str] = None
    title: Optional[str] = None
    meta: dict = Field(default_factory=dict)


class SessionOut(BaseModel):
    id: str
    user_id: Optional[str] = None
    title: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @classmethod
    def from_model(cls, m: ChatSession) -> "SessionOut":
        return cls(
            id=m.id,
            user_id=m.user_id,
            title=m.title,
            status=m.status,
            created_at=m.created_at.isoformat() if m.created_at else None,
            updated_at=m.updated_at.isoformat() if m.updated_at else None,
        )


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    sources: list = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    created_at: Optional[str] = None


# ----- Endpoints -------------------------------------------------------
@router.post("", response_model=SessionOut, summary="Create a new chat session")
async def create_session(
    body: SessionCreateIn,
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    svc = SessionService(db)
    sess = await svc.create_session(user_id=body.user_id, title=body.title, meta=body.meta)
    return SessionOut.from_model(sess)


@admin_router.get("", response_model=List[SessionOut], summary="List chat sessions (admin)")
async def list_sessions(
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> List[SessionOut]:
    svc = SessionService(db)
    rows = await svc.list_sessions(user_id=user_id, limit=limit)
    return [SessionOut.from_model(r) for r in rows]


@admin_router.get("/{session_id}", response_model=SessionOut, summary="Session details (admin)")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)) -> SessionOut:
    svc = SessionService(db)
    sess = await svc.get_session(session_id)
    if sess is None:
        raise NotFoundError(f"Session not found: {session_id}")
    return SessionOut.from_model(sess)


@admin_router.post("/{session_id}/close", summary="Close a session (admin)")
async def close_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    svc = SessionService(db)
    ok = await svc.close_session(session_id)
    if not ok:
        raise NotFoundError(f"Session not found: {session_id}")
    return {"ok": True}


@router.get("/{session_id}/messages", response_model=List[MessageOut])
async def get_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> List[MessageOut]:
    svc = SessionService(db)
    msgs = await svc.get_messages(session_id, limit=limit)
    return [MessageOut(**m.to_dict()) for m in msgs]
