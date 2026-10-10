"""Regression tests for the config/robustness batch (M8+M10+M11+M12+M14).

Covers:
  * Unified error hierarchy — store/embedder errors ARE the AppError classes
    the API handlers understand, and rag_service no longer rewraps them.
  * Runtime temperature — the console-set temperature reaches the RAG path
    (was silently overridden by the .env value).
  * PUT /api/config — field-level merge, secrets never double-obfuscated,
    index_signature not client-writable.
  * Bounded upload read — oversize files rejected without buffering.
  * utcnow_naive helper — naive UTC everywhere timestamps are stored.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.models.base import Base
from src.models.config import CSConfig
from src.utils.obfuscation import deobfuscate

SECRET = "test-secret-key"
ADMIN_USER = "admin"
ADMIN_PASS = "123456"


@pytest.fixture
async def app_client(tmp_path: Path, monkeypatch):
    from src.main import create_app
    from src.services import auth_service

    db_file = tmp_path / "robust_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("APP_SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("APP_MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
    get_settings.cache_clear()
    await dispose_engine()

    app = create_app()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as db:
        await auth_service.ensure_default_admin(db)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await dispose_engine()
    get_settings.cache_clear()


async def _token(client: httpx.AsyncClient) -> dict:
    r = await client.post(
        "/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _stored_config() -> dict:
    async with session_scope() as db:
        row = (await db.execute(select(CSConfig).limit(1))).scalar_one()
        return dict(row.data or {})


# ---------------------------------------------------------------------------
# M12 — one error hierarchy
# ---------------------------------------------------------------------------
def test_error_classes_unified_with_app_error() -> None:
    from src.core.exceptions import AppError
    from src.core.exceptions import EmbeddingError as CoreEmb
    from src.core.exceptions import VectorStoreError as CoreVS
    from src.embeddings.base import EmbeddingError as BaseEmb
    from src.vector_store.base import VectorStoreError as BaseVS

    assert BaseVS is CoreVS, "stores must raise the AppError the handlers know"
    assert BaseEmb is CoreEmb
    assert issubclass(CoreVS, AppError) and issubclass(CoreEmb, AppError)


async def test_retrieve_preserves_original_store_error() -> None:
    """The old `except VectorStoreError: raise` was dead (two different
    classes); now the store's own error passes through unrewrapped."""
    from src.core.exceptions import VectorStoreError
    from src.services.rag_service import RAGService

    class BadStore:
        async def query(self, *a, **kw):
            raise VectorStoreError("milvus down")

        async def aclose(self):
            pass

    class Emb:
        async def embed(self, texts, *, rate_limit_budget=None):
            return [[0.1] * 4 for _ in texts]

        async def aclose(self):
            pass

    rag = RAGService(vector_store=BadStore(), embedder=Emb(), llm=object())
    with pytest.raises(VectorStoreError) as ei:
        await rag._retrieve("q", {"top_k": 3, "similarity_threshold": 0.1})
    assert str(ei.value) == "milvus down", "message must not be rewrapped"
    await rag.aclose()


# ---------------------------------------------------------------------------
# M8 — runtime temperature reaches the RAG path
# ---------------------------------------------------------------------------
async def test_llm_service_resolved_temperature(app_client, monkeypatch) -> None:
    from src.services import llm_service

    async def runtime_09():
        return {"temperature": 0.9}

    monkeypatch.setattr(llm_service, "_load_runtime_config", runtime_09)
    assert await llm_service.LLMService().resolved_temperature() == 0.9

    async def runtime_empty():
        return {}

    monkeypatch.setattr(llm_service, "_load_runtime_config", runtime_empty)
    # .env default (LLM_TEMPERATURE=0.3 in the fixture)
    assert await llm_service.LLMService().resolved_temperature() == 0.3


async def test_rag_caps_document_temp_but_honours_console_value(
    app_client, monkeypatch
) -> None:
    from src.services.rag_service import RAGService

    captured: dict = {}

    class FakeLLM:
        async def resolved_temperature(self) -> float:
            return 0.9

        async def chat(self, messages, *, temperature=None, **kw) -> str:
            captured["temp"] = temperature
            return "answer"

        async def stream_chat(self, messages, *, temperature=None, **kw):
            captured["temp"] = temperature
            yield "answer"

        async def aclose(self):
            pass

    async def with_sources(self, question, history=None, agent_name=None, lang=None):
        return ([], [{"index": 1, "text": "t"}], False)

    async def model_knowledge(self, question, history=None, agent_name=None, lang=None):
        return ([], [], True)

    monkeypatch.setattr(RAGService, "_build_messages", with_sources)
    rag = RAGService(llm=FakeLLM())
    await rag.ask("q")
    assert captured["temp"] == 0.3, "document-grounded answers stay capped"

    monkeypatch.setattr(RAGService, "_build_messages", model_knowledge)
    await rag.ask("q")
    assert captured["temp"] == 0.9, "fallback answers use the console temperature"

    captured.clear()
    monkeypatch.setattr(RAGService, "_build_messages", model_knowledge)
    async for _ in rag.stream_ask("q"):
        pass
    assert captured["temp"] == 0.9
    await rag.aclose()


# ---------------------------------------------------------------------------
# M10 — PUT /api/config merges, secrets and index_signature are safe
# ---------------------------------------------------------------------------
async def test_put_config_partial_merges_and_keeps_secrets(app_client) -> None:
    headers = await _token(app_client)
    r = await app_client.put(
        "/api/config",
        json={
            "name": "客服A",
            "model_api_key": "sk-real-key",
            "embedding": {"api_key": "emb-key", "model": "m1"},
            "vector_db": {"milvus_token": "tok"},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # The reindex pipeline stamps the signature directly on the row.
    sig = {"provider": "openai_compatible", "model": "m1", "base_url": "x", "dim": 4}
    async with session_scope() as db:
        row = (await db.execute(select(CSConfig).limit(1))).scalar_one()
        row.data = {**row.data, "index_signature": sig}

    # A partial PUT (a script editing one field) must not touch anything else.
    r2 = await app_client.put("/api/config", json={"name": "客服B"}, headers=headers)
    assert r2.status_code == 200, r2.text
    data = await _stored_config()
    assert data["name"] == "客服B"
    assert deobfuscate(data["model_api_key"]) == "sk-real-key", "secret must survive a partial PUT"
    assert deobfuscate(data["embedding"]["api_key"]) == "emb-key"
    assert deobfuscate(data["vector_db"]["milvus_token"]) == "tok"
    assert data["index_signature"] == sig


async def test_put_config_roundtrip_neither_double_obfuscates_nor_clobbers(
    app_client,
) -> None:
    headers = await _token(app_client)
    await app_client.put(
        "/api/config", json={"model_api_key": "sk-real-key"}, headers=headers
    )
    sig = {"provider": "p", "model": "m", "base_url": "b", "dim": 8}
    async with session_scope() as db:
        row = (await db.execute(select(CSConfig).limit(1))).scalar_one()
        row.data = {**row.data, "index_signature": sig}

    # The console's GET → edit → PUT round-trip: masked keys echoed back,
    # signature included in the payload.
    payload = (await app_client.get("/api/config", headers=headers)).json()
    assert payload["model_api_key"].startswith("****")
    r = await app_client.put("/api/config", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    data = await _stored_config()
    assert deobfuscate(data["model_api_key"]) == "sk-real-key", "mask echo must not re-obfuscate"
    assert data["index_signature"] == sig

    # A forged signature is ignored.
    r2 = await app_client.put(
        "/api/config",
        json={"index_signature": {"provider": "evil", "model": "e", "base_url": "", "dim": 1}},
        headers=headers,
    )
    assert r2.status_code == 200
    assert (await _stored_config())["index_signature"] == sig, "index_signature is server-owned"


# ---------------------------------------------------------------------------
# M11 — bounded upload read
# ---------------------------------------------------------------------------
async def test_upload_rejects_oversize_file(app_client) -> None:
    headers = await _token(app_client)
    big = b"x" * (2 * 1024 * 1024)  # fixture caps at 1 MB
    r = await app_client.post(
        "/api/documents/upload",
        files={"file": ("big.txt", big, "text/plain")},
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "document_error"

    # A file just under the limit still goes through.
    ok = await app_client.post(
        "/api/documents/upload",
        files={"file": ("small.txt", b"hello", "text/plain")},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# M14 — timestamps
# ---------------------------------------------------------------------------
def test_utcnow_naive_is_naive_utc() -> None:
    from src.utils.timeutil import utcnow_naive

    now = utcnow_naive()
    assert now.tzinfo is None
    aware = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now - aware).total_seconds()) < 2


def test_reindex_now_is_utc_zulu() -> None:
    from src.services.reindex_service import _now

    s = _now()
    assert s.endswith("Z")
    parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 5
