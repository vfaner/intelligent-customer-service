"""Regression tests for the security hardening batch.

Covers:
  * H4 — session enumeration/close is admin-only; chat validates session ids.
  * H5 — login brute-force brake + cached default-password probe.
  * H6 — the repo-default APP_SECRET_KEY never stays in effect.
  * M2 — wildcard CORS no longer combines with credentials.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.core.ratelimit import MAX_FAILURES, login_limiter
from src.core.security import (
    DEFAULT_SECRET,
    DefaultSecretError,
    resolve_secret_key,
)
from src.models.base import Base
from src.services import auth_service

SECRET = "test-secret-key"
ADMIN_USER = "admin"
ADMIN_PASS = "123456"


@pytest.fixture(autouse=True)
def _reset_limiter_and_cache():
    login_limiter.reset()
    auth_service._default_pw_cache.clear()
    yield
    login_limiter.reset()
    auth_service._default_pw_cache.clear()


@pytest.fixture
async def app_client(tmp_path: Path, monkeypatch):
    """A real app instance on a throwaway SQLite file, admin seeded."""
    db_file = tmp_path / "sec_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("APP_SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    await dispose_engine()

    from src.main import create_app

    app = create_app()

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as db:
        await auth_service.ensure_default_admin(db)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await dispose_engine()
    get_settings.cache_clear()


async def _token(client: httpx.AsyncClient) -> str:
    r = await client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
#  H4 — session API surface
# ---------------------------------------------------------------------------

async def test_list_sessions_requires_admin(app_client) -> None:
    r = await app_client.get("/api/sessions")
    assert r.status_code == 401
    token = await _token(app_client)
    r2 = await app_client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200


async def test_close_session_requires_admin(app_client) -> None:
    # Create stays public — the widget needs it.
    r = await app_client.post("/api/sessions", json={"title": "t"})
    assert r.status_code == 200
    sid = r.json()["id"]

    assert (await app_client.post(f"/api/sessions/{sid}/close")).status_code == 401

    token = await _token(app_client)
    r2 = await app_client.post(
        f"/api/sessions/{sid}/close", headers={"Authorization": f"Bearer {token}"}
    )
    assert r2.status_code == 200


async def test_session_details_require_admin_but_messages_stay_capability(app_client) -> None:
    r = await app_client.post("/api/sessions", json={"title": "t"})
    sid = r.json()["id"]
    # Metadata (title = first question) is admin-only…
    assert (await app_client.get(f"/api/sessions/{sid}")).status_code == 401
    # …but reading back a session id you hold stays public (widget history).
    assert (await app_client.get(f"/api/sessions/{sid}/messages")).status_code == 200


async def test_chat_rejects_unknown_session(app_client) -> None:
    r = await app_client.post("/api/chat", json={"message": "hi", "session_id": "does-not-exist"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_chat_rejects_closed_session(app_client) -> None:
    token = await _token(app_client)
    r = await app_client.post("/api/sessions", json={"title": "t"})
    sid = r.json()["id"]
    await app_client.post(f"/api/sessions/{sid}/close", headers={"Authorization": f"Bearer {token}"})

    r2 = await app_client.post("/api/chat", json={"message": "hi", "session_id": sid})
    assert r2.status_code == 422

    r3 = await app_client.post("/api/chat/stream", json={"message": "hi", "session_id": sid})
    assert r3.status_code == 422


# ---------------------------------------------------------------------------
#  H5 — brute-force brake + PBKDF2 amplification
# ---------------------------------------------------------------------------

async def test_login_locks_after_repeated_failures(app_client) -> None:
    for _ in range(MAX_FAILURES):
        r = await app_client.post(
            "/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}
        )
        assert r.status_code == 401
    # Next attempt is throttled before any PBKDF2 work happens.
    r = await app_client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    # Even the CORRECT password is refused while locked.
    r = await app_client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert r.status_code == 429


async def test_successful_login_resets_the_brake(app_client) -> None:
    for _ in range(MAX_FAILURES - 1):
        await app_client.post(
            "/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}
        )
    r = await app_client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert r.status_code == 200
    # Counter wiped: another MAX_FAILURES-1 misses still don't lock.
    for _ in range(MAX_FAILURES - 1):
        r = await app_client.post(
            "/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"}
        )
        assert r.status_code == 401


async def test_default_password_probe_is_cached(app_client, monkeypatch) -> None:
    calls = {"n": 0}
    real_verify = auth_service.verify_password

    def counting_verify(password, hashed):
        calls["n"] += 1
        return real_verify(password, hashed)

    monkeypatch.setattr(auth_service, "verify_password", counting_verify)

    r1 = await app_client.get("/api/auth/state")
    assert r1.status_code == 200 and r1.json()["default_password"] is True
    r2 = await app_client.get("/api/auth/state")
    assert r2.json()["default_password"] is True
    assert calls["n"] == 1, "PBKDF2 must run once per hash, not once per request"


# ---------------------------------------------------------------------------
#  H6 — default secret never stays in effect
# ---------------------------------------------------------------------------

def test_custom_secret_passes_through(tmp_path: Path) -> None:
    f = tmp_path / ".secret_key"
    assert resolve_secret_key("my-own-key", "development", str(f)) == "my-own-key"
    assert not f.exists()


def test_production_refuses_default_secret(tmp_path: Path) -> None:
    with pytest.raises(DefaultSecretError):
        resolve_secret_key(DEFAULT_SECRET, "production", str(tmp_path / ".secret_key"))


def test_default_secret_is_rotated_and_persisted(tmp_path: Path) -> None:
    f = tmp_path / "sub" / ".secret_key"
    s1 = resolve_secret_key(DEFAULT_SECRET, "development", str(f))
    assert s1 and s1 != DEFAULT_SECRET and len(s1) >= 32
    assert f.exists()
    # Owner-only permissions.
    assert f.stat().st_mode & 0o077 == 0
    # A restart (second call) adopts the persisted key — tokens stay valid.
    s2 = resolve_secret_key(DEFAULT_SECRET, "development", str(f))
    assert s2 == s1


# ---------------------------------------------------------------------------
#  M2 — CORS wildcard must not carry credentials
# ---------------------------------------------------------------------------

def _cors_kwargs(app) -> dict:
    from fastapi.middleware.cors import CORSMiddleware

    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return mw.kwargs
    raise AssertionError("CORSMiddleware not installed")


def test_wildcard_cors_disables_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("CORS_ORIGINS", "*")
    get_settings.cache_clear()
    from src.main import create_app

    assert _cors_kwargs(create_app())["allow_credentials"] is False
    get_settings.cache_clear()


def test_explicit_cors_origins_keep_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("CORS_ORIGINS", "https://shop.example")
    get_settings.cache_clear()
    from src.main import create_app

    kwargs = _cors_kwargs(create_app())
    assert kwargs["allow_credentials"] is True
    assert kwargs["allow_origins"] == ["https://shop.example"]
    get_settings.cache_clear()
