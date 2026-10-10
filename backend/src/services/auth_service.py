"""Admin account workflows: seeding, login, profile and password changes."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.exceptions import AuthError, ValidationError
from ..core.security import (
    create_token,
    hash_password,
    verify_password,
)
from ..models.user import AdminUser

log = logging.getLogger(__name__)

MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32

# password_hash -> "is it still the factory default"; see
# is_default_password_in_use for why this exists.
_default_pw_cache: dict = {}


async def ensure_default_admin(db: AsyncSession) -> Optional[AdminUser]:
    """Create the seed account when the user table is empty. Idempotent.

    Keyed on "table is empty", not "no user named admin": if the operator
    renamed the account, re-seeding `admin` would silently hand out a second
    login with a default password.
    """
    settings = get_settings()
    count = (await db.execute(select(func.count()).select_from(AdminUser))).scalar_one()
    if count:
        return None

    user = AdminUser(
        username=settings.auth.default_username,
        password_hash=hash_password(settings.auth.default_password),
        display_name="管理员",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    log.warning(
        "Seeded default admin account %r with password %r — change it in "
        "「用户信息」 (admin console) before exposing this server.",
        settings.auth.default_username,
        settings.auth.default_password,
    )
    return user


async def get_by_username(db: AsyncSession, username: str) -> Optional[AdminUser]:
    stmt = select(AdminUser).where(AdminUser.username == username)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_by_id(db: AsyncSession, user_id: int) -> Optional[AdminUser]:
    return (await db.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one_or_none()


def issue_token(user: AdminUser) -> Tuple[str, int]:
    """Sign a fresh token for ``user``. Returns ``(token, expires_in)``."""
    settings = get_settings()
    return create_token(
        user_id=user.id,
        username=user.username,
        password_hash=user.password_hash,
        secret=settings.app.secret_key,
        ttl_seconds=settings.auth.token_ttl_hours * 3600,
    )


async def authenticate(db: AsyncSession, username: str, password: str) -> AdminUser:
    """Verify credentials and stamp ``last_login_at``.

    Every failure path raises the same message on purpose. Saying "no such user"
    versus "wrong password" hands an attacker a username oracle, and the admin
    typing their own password gains nothing from the distinction.
    """
    generic = AuthError("用户名或密码错误")

    user = await get_by_username(db, (username or "").strip())
    if user is None or not user.is_active:
        # Still burn a hash so a missing user isn't measurably faster than a
        # wrong password.
        verify_password(password or "x", hash_password("timing-equalizer"))
        raise generic
    if not verify_password(password or "", user.password_hash):
        raise generic

    user.last_login_at = datetime.now()
    await db.flush()
    return user


async def is_default_password_in_use(db: AsyncSession) -> bool:
    """True when the seed account still has its factory password.

    Drives the login page's "default credentials" hint and the console's nag
    banner — both of which must disappear the moment the password is changed.

    The result is cached per password hash: GET /api/auth/state is public and
    unauthenticated, so without the cache every request would burn a full
    260k-round PBKDF2 verification — a free CPU-exhaustion amplifier. Keying
    on the hash means changing the password invalidates the entry naturally.
    """
    settings = get_settings()
    user = await get_by_username(db, settings.auth.default_username)
    if user is None:
        return False
    cached = _default_pw_cache.get(user.password_hash)
    if cached is None:
        cached = verify_password(settings.auth.default_password, user.password_hash)
        if len(_default_pw_cache) > 16:  # hashes only accumulate on changes
            _default_pw_cache.clear()
        _default_pw_cache[user.password_hash] = cached
    return cached


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not (MIN_USERNAME_LEN <= len(username) <= MAX_USERNAME_LEN):
        raise ValidationError(f"用户名长度需在 {MIN_USERNAME_LEN}-{MAX_USERNAME_LEN} 个字符之间")
    if any(c.isspace() for c in username):
        raise ValidationError("用户名不能包含空格")
    return username


async def update_profile(
    db: AsyncSession,
    user: AdminUser,
    *,
    username: Optional[str] = None,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
) -> AdminUser:
    """Update the signed-in account's own details.

    Note the username is part of the token payload, so the caller must re-issue
    a token when it changes — see ``api/auth.py``.
    """
    if username is not None:
        new_name = _validate_username(username)
        if new_name != user.username:
            clash = await get_by_username(db, new_name)
            if clash is not None:
                raise ValidationError("该用户名已被占用")
            user.username = new_name
    if display_name is not None:
        user.display_name = display_name.strip()[:128]
    if email is not None:
        email = email.strip()[:256]
        if email and "@" not in email:
            raise ValidationError("邮箱格式不正确")
        user.email = email
    await db.flush()
    return user


async def change_password(
    db: AsyncSession, user: AdminUser, *, old_password: str, new_password: str
) -> AdminUser:
    """Verify the current password, then replace it.

    Requiring the old password is what stops a stolen token from becoming
    permanent account takeover.
    """
    if not verify_password(old_password or "", user.password_hash):
        raise ValidationError("原密码不正确")
    new_password = new_password or ""
    if not (MIN_PASSWORD_LEN <= len(new_password) <= MAX_PASSWORD_LEN):
        raise ValidationError(f"新密码长度需在 {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} 个字符之间")
    if verify_password(new_password, user.password_hash):
        raise ValidationError("新密码不能与原密码相同")

    user.password_hash = hash_password(new_password)
    await db.flush()
    # Every previously issued token embeds the old password fingerprint (`pv`)
    # and is now rejected — other devices are signed out, which is the point.
    log.info("Password changed for admin user %r; existing tokens invalidated", user.username)
    return user
