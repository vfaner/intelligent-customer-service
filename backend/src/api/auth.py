"""Admin authentication API: login, logout, profile, password.

The login response hands the token back **twice**:

* in the JSON body — the admin console stores it and sends it as
  ``Authorization: Bearer``, which is what makes a cross-origin console
  (``/admin/?api=https://other-host``) work at all;
* as an HttpOnly cookie — which is what lets the static-file middleware in
  ``main.py`` gate ``/admin/index.html`` before the browser ever runs any JS.

Same string, two transports. Anything that changes the token (renaming the
account, changing the password) has to refresh both, or the caller gets signed
out by their own successful request.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.auth import current_user_or_401
from ..core.config import get_settings
from ..core.database import get_db
from ..core.exceptions import AuthError, RateLimitError
from ..core.ratelimit import login_limiter
from ..models.user import AdminUser
from ..services import auth_service

router = APIRouter()


# ---------------------------------------------------------------------------
#  Schemas
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str = ""
    email: str = ""
    is_active: bool = True
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ProfileIn(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    email: Optional[str] = None


class PasswordIn(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=1, max_length=128)


class StateOut(BaseModel):
    # False when AUTH_ENABLED=false: the login page then says so instead of
    # pretending a password is required.
    auth_required: bool
    # Drives the "first run: admin / 123456" hint. Goes away permanently once
    # the password is changed, so a deployed server stops advertising it.
    default_password: bool


def _user_out(user: AdminUser) -> UserOut:
    """Serialise the account, field by field.

    Deliberately not ``user.to_dict()``: that also reads ``updated_at``, which
    the ``onupdate=func.now()`` flush in login/profile-update has just expired.
    Touching it here would trigger a lazy refresh outside SQLAlchemy's async
    greenlet and blow up with MissingGreenlet. Listing the fields also means the
    password hash can't be swept into a response by accident.
    """
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name or "",
        email=user.email or "",
        is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


def _set_cookie(response: Response, token: str, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.auth.cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,          # JS never needs it; the console uses the header
        samesite="lax",         # sent on top-level navigation to /admin/, not on cross-site POSTs
        path="/",
        # Not `secure=True` unconditionally: that would break plain-HTTP local
        # installs (the default way this project is run). Behind HTTPS, set
        # APP_ENV=production and put a reverse proxy in front.
        secure=settings.app.env == "production",
    )


# ---------------------------------------------------------------------------
#  Endpoints
# ---------------------------------------------------------------------------


@router.get("/state", response_model=StateOut, summary="Whether login is required (public)")
async def auth_state(db: AsyncSession = Depends(get_db)) -> StateOut:
    settings = get_settings()
    default_pw = False
    if settings.auth.enabled:
        try:
            default_pw = await auth_service.is_default_password_in_use(db)
        except Exception:
            # Fresh install, table not created yet — the hint just stays hidden.
            default_pw = False
    return StateOut(auth_required=settings.auth.enabled, default_password=default_pw)


@router.post("/login", response_model=TokenOut, summary="Sign in to the admin console")
async def login(
    body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenOut:
    # Brake on credential stuffing: two keys per attempt, see core/ratelimit.
    ip = request.client.host if request.client else "unknown"
    keys = (f"ip:{ip}", f"ip:{ip}|user:{body.username.strip().lower()}")
    for key in keys:
        remaining = login_limiter.check(key)
        if remaining > 0:
            minutes = int(remaining // 60) + 1
            raise RateLimitError(f"尝试次数过多，请 {minutes} 分钟后再试")
    try:
        user = await auth_service.authenticate(db, body.username, body.password)
    except AuthError:
        for key in keys:
            login_limiter.record_failure(key)
        raise
    for key in keys:
        login_limiter.record_success(key)
    token, expires_in = auth_service.issue_token(user)
    _set_cookie(response, token, expires_in)
    return TokenOut(access_token=token, expires_in=expires_in, user=_user_out(user))


@router.post("/logout", summary="Sign out (clears the cookie)")
async def logout(response: Response) -> dict:
    """Drops the cookie. The console also discards its stored token.

    Tokens are stateless, so a copy captured elsewhere stays valid until it
    expires — that is the trade for having no session store. Change the password
    to revoke everything immediately.
    """
    response.delete_cookie(key=get_settings().auth.cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut, summary="Current account")
async def me(user: AdminUser = Depends(current_user_or_401)) -> UserOut:
    return _user_out(user)


@router.put("/me", response_model=TokenOut, summary="Update the current account's details")
async def update_me(
    body: ProfileIn,
    response: Response,
    user: AdminUser = Depends(current_user_or_401),
    db: AsyncSession = Depends(get_db),
) -> TokenOut:
    """Returns a **fresh token** carrying the new username.

    A rename does not invalidate anything — tokens resolve the account by ``uid``
    — so the old one keeps working. Re-issuing just stops the client from holding
    a token whose ``sub`` no longer matches the account it belongs to.
    """
    await auth_service.update_profile(
        db,
        user,
        username=body.username,
        display_name=body.display_name,
        email=body.email,
    )
    token, expires_in = auth_service.issue_token(user)
    _set_cookie(response, token, expires_in)
    return TokenOut(access_token=token, expires_in=expires_in, user=_user_out(user))


@router.put("/me/password", response_model=TokenOut, summary="Change the current password")
async def update_password(
    body: PasswordIn,
    response: Response,
    user: AdminUser = Depends(current_user_or_401),
    db: AsyncSession = Depends(get_db),
) -> TokenOut:
    """Returns a fresh token — the change invalidates every existing one.

    Without re-issuing here, the admin would be logged out by their own password
    change (their token embeds the old password fingerprint).
    """
    await auth_service.change_password(
        db, user, old_password=body.old_password, new_password=body.new_password
    )
    token, expires_in = auth_service.issue_token(user)
    _set_cookie(response, token, expires_in)
    return TokenOut(access_token=token, expires_in=expires_in, user=_user_out(user))


@router.get("/ping", summary="Cheap 'is my token still valid' check")
async def ping(user: AdminUser = Depends(current_user_or_401)) -> dict:
    return {"ok": True, "username": user.username}
