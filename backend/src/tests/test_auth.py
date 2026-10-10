"""Admin authentication: hashing, tokens, and the endpoint protection matrix.

The matrix tests are the important ones. Which endpoints are public is a product
decision (the chat widget runs on strangers' websites and must keep working;
everything the console drives must not), and nothing else in the codebase records
it. A future refactor that moves a router or forgets `dependencies=[...]` breaks
one of these assertions instead of silently exposing the API-key configuration.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from src.core import security
from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.models.base import Base
from src.services import auth_service

DEFAULT_USER = "admin"
DEFAULT_PASS = "123456"


# ---------------------------------------------------------------------------
#  Password hashing
# ---------------------------------------------------------------------------


def test_password_round_trip() -> None:
    stored = security.hash_password("s3cret-pw")
    assert security.verify_password("s3cret-pw", stored)
    assert not security.verify_password("s3cret-pX", stored)
    assert not security.verify_password("", stored)


def test_password_hash_is_salted() -> None:
    """Two hashes of the same password must differ, or the DB leaks reuse."""
    a = security.hash_password("same-password")
    b = security.hash_password("same-password")
    assert a != b
    assert security.verify_password("same-password", a)
    assert security.verify_password("same-password", b)


def test_password_hash_format() -> None:
    algo, iters, salt, digest = security.hash_password("x").split("$")
    assert algo == "pbkdf2_sha256"
    assert int(iters) >= 200_000, "iteration count must not be lowered"
    assert len(salt) == 32 and len(digest) == 64


def test_verify_password_tolerates_garbage() -> None:
    """A corrupted row must lock the account, not 500 the login endpoint."""
    for bad in ("", "not-a-hash", "pbkdf2_sha256$abc$def", "md5$1$aa$bb", "$$$"):
        assert security.verify_password("anything", bad) is False


# ---------------------------------------------------------------------------
#  Tokens
# ---------------------------------------------------------------------------

SECRET = "test-secret-key"


def _token(**over):
    kwargs = dict(
        user_id=1, username="admin", password_hash="hash-a", secret=SECRET, ttl_seconds=3600
    )
    kwargs.update(over)
    return security.create_token(**kwargs)


def test_token_round_trip() -> None:
    token, expires_in = _token()
    assert expires_in == 3600
    payload = security.decode_token(token, SECRET)
    assert payload["sub"] == "admin"
    assert payload["uid"] == 1
    assert payload["pv"] == security.password_version("hash-a")
    assert payload["exp"] > time.time()


def test_token_rejects_wrong_secret() -> None:
    token, _ = _token()
    with pytest.raises(security.TokenError):
        security.decode_token(token, "another-secret")


def test_token_rejects_tampered_payload() -> None:
    """Swapping the payload without re-signing must not authenticate."""
    token, _ = _token()
    forged, _ = _token(username="root", secret="attacker-secret")
    body = forged.split(".")[0]
    sig = token.split(".")[1]
    with pytest.raises(security.TokenError):
        security.decode_token(f"{body}.{sig}", SECRET)


def test_token_rejects_expired() -> None:
    token, _ = _token(ttl_seconds=-1)
    with pytest.raises(security.TokenError):
        security.decode_token(token, SECRET)


def test_token_rejects_malformed() -> None:
    for bad in ("", "abc", "a.b.c.d", "...", "not$a%token"):
        with pytest.raises(security.TokenError):
            security.decode_token(bad, SECRET)


def test_password_version_changes_with_password() -> None:
    """This is what signs other devices out when the password changes."""
    assert security.password_version("hash-a") != security.password_version("hash-b")
    assert security.password_version("hash-a") == security.password_version("hash-a")


# ---------------------------------------------------------------------------
#  HTTP-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def app_client(tmp_path: Path, monkeypatch):
    """A real app instance on a throwaway SQLite file.

    ASGITransport does not run lifespan events, so the table creation and admin
    seeding that ``main.lifespan`` normally does happen here by hand.
    """
    db_file = tmp_path / "auth_test.db"
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


async def _login(client: httpx.AsyncClient, user=DEFAULT_USER, pw=DEFAULT_PASS) -> str:
    r = await client.post("/api/auth/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
#  Seeding and login
# ---------------------------------------------------------------------------


async def test_default_admin_is_seeded(app_client: httpx.AsyncClient) -> None:
    token = await _login(app_client)
    me = await app_client.get("/api/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["username"] == DEFAULT_USER


async def test_seeding_is_idempotent(app_client: httpx.AsyncClient) -> None:
    """Re-seeding must not create a second account with a default password."""
    from sqlalchemy import func, select

    from src.models.user import AdminUser

    async with session_scope() as db:
        await auth_service.ensure_default_admin(db)
        await auth_service.ensure_default_admin(db)
        n = (await db.execute(select(func.count()).select_from(AdminUser))).scalar_one()
    assert n == 1


async def test_login_sets_cookie(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post(
        "/api/auth/login", json={"username": DEFAULT_USER, "password": DEFAULT_PASS}
    )
    assert r.status_code == 200
    assert get_settings().auth.cookie_name in r.cookies
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == get_settings().auth.token_ttl_hours * 3600


async def test_login_rejects_wrong_password(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post(
        "/api/auth/login", json={"username": DEFAULT_USER, "password": "wrong"}
    )
    assert r.status_code == 401


async def test_login_does_not_leak_whether_user_exists(app_client: httpx.AsyncClient) -> None:
    """Same status and message either way — no username enumeration oracle."""
    bad_pw = await app_client.post(
        "/api/auth/login", json={"username": DEFAULT_USER, "password": "wrong"}
    )
    no_user = await app_client.post(
        "/api/auth/login", json={"username": "ghost", "password": "wrong"}
    )
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json()["error"]["message"] == no_user.json()["error"]["message"]


async def test_auth_state_reports_default_password(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/api/auth/state")
    assert r.status_code == 200
    assert r.json() == {"auth_required": True, "default_password": True}

    token = await _login(app_client)
    await app_client.put(
        "/api/auth/me/password",
        headers=_auth(token),
        json={"old_password": DEFAULT_PASS, "new_password": "brand-new-pw"},
    )
    r2 = await app_client.get("/api/auth/state")
    assert r2.json()["default_password"] is False, "hint must vanish once changed"


# ---------------------------------------------------------------------------
#  The protection matrix
# ---------------------------------------------------------------------------

PROTECTED = [
    ("GET", "/api/config"),
    ("GET", "/api/config/providers"),
    ("GET", "/api/documents/list"),
    ("GET", "/api/documents/parsers"),
    ("GET", "/api/models"),
    ("GET", "/api/models/vector-db"),
    ("GET", "/api/admin/status"),
    ("GET", "/api/auth/me"),
    ("GET", "/api/sessions"),
]

# Public because a visitor on a third-party website calls them.
PUBLIC = [
    ("GET", "/health"),
    ("GET", "/api/config/public"),
    ("GET", "/api/auth/state"),
]

# Every route in the app, classified. The lists above are the ones actually
# exercised over HTTP (they need no request body); this one exists so that
# *adding* an endpoint is a decision rather than an accident — see
# test_every_route_is_classified below.
#
# Keys are the FastAPI path templates as they appear in the OpenAPI schema.
ROUTE_CLASSIFICATION = {
    # --- open to anyone -----------------------------------------------------
    ("GET", "/"): "public",
    ("GET", "/health"): "public",
    ("GET", "/api/config/public"): "public",
    ("GET", "/api/auth/state"): "public",
    ("POST", "/api/auth/login"): "public",
    ("POST", "/api/auth/logout"): "public",
    # The chat widget: runs in a stranger's browser with no credential to offer.
    ("POST", "/api/chat"): "public",
    ("POST", "/api/chat/stream"): "public",
    ("POST", "/api/sessions"): "public",
    # Reading back the messages of a session id the caller already holds is a
    # capability URL — that is what lets an embedded widget restore its own
    # history without an account.
    ("GET", "/api/sessions/{session_id}/messages"): "public",
    # --- admin only ---------------------------------------------------------
    # Enumerating/closing OTHER people's sessions used to be public, which let
    # any anonymous visitor dump every customer conversation.
    ("GET", "/api/sessions"): "protected",
    ("GET", "/api/sessions/{session_id}"): "protected",
    ("POST", "/api/sessions/{session_id}/close"): "protected",
    ("GET", "/api/auth/me"): "protected",
    ("PUT", "/api/auth/me"): "protected",
    ("PUT", "/api/auth/me/password"): "protected",
    ("GET", "/api/auth/ping"): "protected",
    ("GET", "/api/config"): "protected",
    ("PUT", "/api/config"): "protected",
    ("GET", "/api/config/providers"): "protected",
    ("GET", "/api/documents/list"): "protected",
    ("GET", "/api/documents/parsers"): "protected",
    ("POST", "/api/documents/upload"): "protected",
    ("POST", "/api/documents/process"): "protected",
    ("POST", "/api/documents/split-preview"): "protected",
    ("POST", "/api/documents/reindex"): "protected",
    ("GET", "/api/documents/reindex/status"): "protected",
    ("DELETE", "/api/documents/{document_id}"): "protected",
    ("GET", "/api/models"): "protected",
    ("GET", "/api/models/vector-db"): "protected",
    ("POST", "/api/models/available"): "protected",
    ("POST", "/api/models/test"): "protected",
    ("POST", "/api/models/test-embedding"): "protected",
    ("GET", "/api/admin/status"): "protected",
    ("GET", "/api/admin/version"): "protected",
    ("POST", "/api/admin/init"): "protected",
}


def test_every_route_is_classified() -> None:
    """A new endpoint must be deliberately placed on one side of the fence.

    Guards are applied per router, so a new endpoint inherits whatever its
    router has — which is the right default, but it also means someone can add
    a route to `sessions` (public) without noticing it reads the API keys. This
    test fails on any route that is not listed above, forcing the question to be
    answered once, in writing.
    """
    from src.main import app

    actual = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if method.upper() not in ("HEAD", "OPTIONS")
    }
    unclassified = actual - set(ROUTE_CLASSIFICATION)
    assert not unclassified, (
        "new endpoint(s) not in ROUTE_CLASSIFICATION — decide whether each needs "
        f"a login and add it there: {sorted(unclassified)}"
    )
    stale = set(ROUTE_CLASSIFICATION) - actual
    assert not stale, f"ROUTE_CLASSIFICATION lists endpoints that no longer exist: {sorted(stale)}"


@pytest.mark.parametrize(
    "method,path",
    [k for k, v in ROUTE_CLASSIFICATION.items() if v == "protected" and "{" not in k[1]],
)
async def test_classified_protected_routes_reject_anonymous(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    """Reject before validating: no body is sent, so a 422 would mean the guard
    ran too late (or not at all) and the endpoint parsed input from a stranger."""
    r = await app_client.request(method, path)
    assert r.status_code == 401, f"{method} {path} must require login, got {r.status_code}"


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_protected_endpoints_reject_anonymous(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    r = await app_client.request(method, path)
    assert r.status_code == 401, f"{path} must require login, got {r.status_code}"
    assert r.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("method,path", PROTECTED)
async def test_protected_endpoints_accept_token(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    token = await _login(app_client)
    r = await app_client.request(method, path, headers=_auth(token))
    assert r.status_code == 200, f"{path} rejected a valid token: {r.text[:200]}"


@pytest.mark.parametrize("method,path", PUBLIC)
async def test_public_endpoints_stay_open(
    app_client: httpx.AsyncClient, method: str, path: str
) -> None:
    r = await app_client.request(method, path)
    assert r.status_code != 401, f"{path} must stay reachable without login"


async def test_cookie_also_authenticates_api(app_client: httpx.AsyncClient) -> None:
    """Login sets the cookie; the client keeps it, so no header is needed."""
    await _login(app_client)
    r = await app_client.get("/api/config")
    assert r.status_code == 200


async def test_public_config_hides_secrets(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/api/config/public")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "name", "avatar", "welcome_message", "contact_phone", "contact_email",
    }
    for leaky in ("model_api_key", "model_base_url", "embedding", "vector_db", "rag"):
        assert leaky not in body


async def test_chat_endpoints_need_no_login(app_client: httpx.AsyncClient) -> None:
    """The widget must work for anonymous visitors.

    A chat call with no LLM configured fails for its own reasons — the point is
    only that it isn't turned away at the door with a 401.
    """
    r = await app_client.post("/api/sessions", json={"user_id": "anon"})
    assert r.status_code != 401
    r2 = await app_client.post("/api/chat", json={"session_id": "nope", "message": "hi"})
    assert r2.status_code != 401


# ---------------------------------------------------------------------------
#  Page gate
# ---------------------------------------------------------------------------


async def test_admin_page_redirects_to_login(app_client: httpx.AsyncClient) -> None:
    for path in ("/admin/", "/admin/index.html"):
        r = await app_client.get(path)
        assert r.status_code == 302, f"{path} should redirect anonymous visitors"
        assert "/admin/login.html" in r.headers["location"]
        assert "next=" in r.headers["location"]


async def test_login_page_is_reachable(app_client: httpx.AsyncClient) -> None:
    """Gating the login page itself would be an infinite redirect."""
    r = await app_client.get("/admin/login.html")
    assert r.status_code == 200


async def test_embed_page_is_reachable(app_client: httpx.AsyncClient) -> None:
    """Third-party sites iframe this. A redirect here breaks every embed."""
    r = await app_client.get("/admin/embed.html")
    assert r.status_code == 200
    r2 = await app_client.get("/embed")
    assert r2.status_code == 307
    assert "login" not in r2.headers["location"]


async def test_admin_page_opens_with_cookie(app_client: httpx.AsyncClient) -> None:
    await _login(app_client)
    r = await app_client.get("/admin/index.html")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
#  Profile and password
# ---------------------------------------------------------------------------


async def test_update_profile(app_client: httpx.AsyncClient) -> None:
    token = await _login(app_client)
    r = await app_client.put(
        "/api/auth/me",
        headers=_auth(token),
        json={"display_name": "运维小张", "email": "ops@example.com"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["display_name"] == "运维小张"
    assert r.json()["user"]["email"] == "ops@example.com"


async def test_rename_account_returns_working_token(app_client: httpx.AsyncClient) -> None:
    """The username is in the token payload, so a rename must re-issue one."""
    token = await _login(app_client)
    r = await app_client.put("/api/auth/me", headers=_auth(token), json={"username": "boss"})
    assert r.status_code == 200
    new_token = r.json()["access_token"]

    me = await app_client.get("/api/auth/me", headers=_auth(new_token))
    assert me.json()["username"] == "boss"
    assert await _login(app_client, user="boss", pw=DEFAULT_PASS)


async def test_profile_rejects_bad_input(app_client: httpx.AsyncClient) -> None:
    token = await _login(app_client)
    for payload in ({"username": "ab"}, {"username": "a b c"}, {"email": "nope"}):
        r = await app_client.put("/api/auth/me", headers=_auth(token), json=payload)
        assert r.status_code == 422, payload


async def test_change_password_flow(app_client: httpx.AsyncClient) -> None:
    token = await _login(app_client)

    wrong = await app_client.put(
        "/api/auth/me/password",
        headers=_auth(token),
        json={"old_password": "not-it", "new_password": "whatever-123"},
    )
    assert wrong.status_code == 422

    short = await app_client.put(
        "/api/auth/me/password",
        headers=_auth(token),
        json={"old_password": DEFAULT_PASS, "new_password": "abc"},
    )
    assert short.status_code == 422

    same = await app_client.put(
        "/api/auth/me/password",
        headers=_auth(token),
        json={"old_password": DEFAULT_PASS, "new_password": DEFAULT_PASS},
    )
    assert same.status_code == 422

    ok = await app_client.put(
        "/api/auth/me/password",
        headers=_auth(token),
        json={"old_password": DEFAULT_PASS, "new_password": "new-strong-pw"},
    )
    assert ok.status_code == 200

    # New password works, old one does not.
    assert await _login(app_client, pw="new-strong-pw")
    bad = await app_client.post(
        "/api/auth/login", json={"username": DEFAULT_USER, "password": DEFAULT_PASS}
    )
    assert bad.status_code == 401


async def test_password_change_invalidates_old_tokens(app_client: httpx.AsyncClient) -> None:
    """Sign out other devices — the whole reason tokens carry `pv`."""
    old_token = await _login(app_client)
    assert (await app_client.get("/api/config", headers=_auth(old_token))).status_code == 200

    fresh = await app_client.put(
        "/api/auth/me/password",
        headers=_auth(old_token),
        json={"old_password": DEFAULT_PASS, "new_password": "rotated-pw-1"},
    )
    new_token = fresh.json()["access_token"]

    stale = await app_client.get("/api/config", headers=_auth(old_token))
    assert stale.status_code == 401, "a token minted before the change must stop working"
    # ...but the device that performed the change keeps working.
    assert (await app_client.get("/api/config", headers=_auth(new_token))).status_code == 200


async def test_logout_clears_cookie(app_client: httpx.AsyncClient) -> None:
    await _login(app_client)
    assert (await app_client.get("/admin/index.html")).status_code == 200
    await app_client.post("/api/auth/logout")
    r = await app_client.get("/admin/index.html")
    assert r.status_code == 302


# ---------------------------------------------------------------------------
#  Escape hatch
# ---------------------------------------------------------------------------


@pytest.fixture
async def open_client(tmp_path: Path, monkeypatch):
    """An instance with AUTH_ENABLED=false."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'open.db'}")
    monkeypatch.setenv("APP_SECRET_KEY", SECRET)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    await dispose_engine()

    from src.main import create_app

    app = create_app()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as db:
        await auth_service.ensure_default_admin(db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await dispose_engine()
    get_settings.cache_clear()


async def test_auth_disabled_opens_everything(open_client: httpx.AsyncClient) -> None:
    assert (await open_client.get("/api/config")).status_code == 200
    assert (await open_client.get("/api/admin/status")).status_code == 200
    assert (await open_client.get("/admin/index.html")).status_code == 200
    assert (await open_client.get("/api/auth/state")).json()["auth_required"] is False
