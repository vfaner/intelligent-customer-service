"""Regression tests for the performance batch (H7 + M5 + M6 + M13).

Covers:
  * services.runtime — fingerprint-keyed singletons, retire, invalidate.
  * LLMService / EmbeddingService — one shared provider/embedder per config,
    rebuilt (not restarted) when the config changes; ephemeral override path.
  * Embedding rate-limit budget — the interactive query path fails in seconds
    instead of riding out the ingestion budget.
  * get_shared_vector_store — cached construction, dim-aware fingerprint.
  * POST /api/documents/process — 202 + background ingest + double-submit
    guard + truthful status in the queued response.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import httpx
import pytest

from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.models.base import Base
from src.models.document import Document
from src.services import runtime
from src.services.embedding_service import QUERY_RATE_LIMIT_BUDGET

SECRET = "test-secret-key"
ADMIN_USER = "admin"
ADMIN_PASS = "123456"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class FakeProvider:
    protocol = "openai"

    def __init__(self, tag: str = "") -> None:
        self.tag = tag
        self.closed = False

    async def chat(self, messages, **kw) -> str:
        return "ok"

    async def stream_chat(self, messages, **kw):
        yield "ok"

    async def test(self):
        return True, "ok"

    async def aclose(self) -> None:
        self.closed = True


class FakeEmbedder:
    dim = 4
    model = "fake"

    def __init__(self) -> None:
        self.closed = False
        self.last_budget: Optional[float] = None
        self.calls = 0

    async def embed(self, texts: List[str], *, rate_limit_budget: Optional[float] = None):
        self.calls += 1
        self.last_budget = rate_limit_budget
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def aclose(self) -> None:
        self.closed = True


class FakeStore:
    name = "fake"

    def __init__(self) -> None:
        self.closed = False

    async def add(self, document_id, chunks, vectors):
        return len(chunks)

    async def query(self, vector, top_k=5, threshold=0.0, where=None):
        return []

    async def delete_document(self, document_id):
        return 0

    async def reset(self):
        pass

    async def count(self):
        return 0

    async def aclose(self):
        self.closed = True


# ---------------------------------------------------------------------------
# runtime cache
# ---------------------------------------------------------------------------
async def test_get_shared_reuses_object_until_fingerprint_changes(monkeypatch) -> None:
    builds = {"n": 0}

    async def build():
        builds["n"] += 1
        return FakeEmbedder()

    a = await runtime.get_shared("t1", "fp-a", build)
    b = await runtime.get_shared("t1", "fp-a", build)
    assert a is b and builds["n"] == 1

    c = await runtime.get_shared("t1", "fp-b", build)
    assert c is not a and builds["n"] == 2


async def test_config_change_retires_and_closes_old_object(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "RETIRE_DELAY", 0.01)
    old = FakeEmbedder()

    async def build_old():
        return old

    async def build_new():
        return FakeEmbedder()

    await runtime.get_shared("t2", "fp-1", build_old)
    await runtime.get_shared("t2", "fp-2", build_new)
    assert old.closed is False           # grace period: in-flight calls survive
    await asyncio.sleep(0.05)
    assert old.closed is True


async def test_invalidate_drops_slot_and_close_shared_closes_all(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "RETIRE_DELAY", 0.01)
    s1 = FakeStore()

    async def build1():
        return s1

    got = await runtime.get_shared("t3", "fp", build1)
    assert got is s1
    runtime.invalidate("t3")
    assert runtime.peek("t3") is None
    await asyncio.sleep(0.05)
    assert s1.closed is True

    s2 = FakeStore()

    async def build2():
        return s2

    await runtime.get_shared("t4", "fp", build2)
    await runtime.close_shared()
    assert s2.closed is True
    assert runtime.peek("t4") is None


# ---------------------------------------------------------------------------
# LLMService — shared provider
# ---------------------------------------------------------------------------
@pytest.fixture
def env_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/perf.db")
    monkeypatch.setenv("APP_SECRET_KEY", SECRET)
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_llm_provider_is_shared_and_rebuilt_on_config_change(
    env_only, monkeypatch
) -> None:
    from src.services import llm_service
    from src.adapters.base import LLMMessage

    built: List[FakeProvider] = []
    monkeypatch.setattr(
        llm_service, "build_provider", lambda **kw: (built.append(FakeProvider()), built[-1])[1]
    )
    cfg = {"model_name": "m1"}

    async def fake_runtime():
        return cfg

    monkeypatch.setattr(llm_service, "_load_runtime_config", fake_runtime)

    msgs = [LLMMessage(role="user", content="hi")]
    await llm_service.LLMService().chat(msgs)
    await llm_service.LLMService().chat(msgs)
    assert len(built) == 1, "same config must reuse the provider (connection pool)"
    assert built[0].closed is False, "shared provider must not be closed per call"

    cfg = {"model_name": "m2"}
    await llm_service.LLMService().chat(msgs)
    assert len(built) == 2, "a config change must take effect without restart"


async def test_llm_override_path_stays_ephemeral(env_only, monkeypatch) -> None:
    from src.services import llm_service
    from src.adapters.base import LLMMessage

    built: List[FakeProvider] = []
    monkeypatch.setattr(
        llm_service, "build_provider", lambda **kw: (built.append(FakeProvider()), built[-1])[1]
    )
    msgs = [LLMMessage(role="user", content="hi")]
    svc = llm_service.LLMService()
    await svc.chat(msgs, override={"model_name": "unsaved"})
    await svc.chat(msgs, override={"model_name": "unsaved"})
    assert len(built) == 2, "override configs must not evict the live provider"
    assert all(p.closed for p in built), "ephemeral providers are closed by the caller"
    assert runtime.peek("llm_provider") is None


# ---------------------------------------------------------------------------
# EmbeddingService — shared embedder + budget passthrough
# ---------------------------------------------------------------------------
async def test_embedder_is_shared_and_not_closed_per_request(env_only, monkeypatch) -> None:
    from src.services import embedding_service

    fakes: List[FakeEmbedder] = []
    cfg = {"provider": "openai_compatible", "api_key": "k", "base_url": "http://e", "model": "m", "dim": 4}

    async def fake_load(settings=None):
        return dict(cfg)

    monkeypatch.setattr(embedding_service, "load_embedding_config", fake_load)
    monkeypatch.setattr(
        embedding_service, "build_embedder_from",
        lambda c: (fakes.append(FakeEmbedder()), fakes[-1])[1],
    )

    s1 = embedding_service.EmbeddingService()
    s2 = embedding_service.EmbeddingService()
    await s1.embed(["a"])
    await s2.embed(["b"])
    assert len(fakes) == 1, "same config must reuse the embedder (connection pool)"
    await s1.aclose()
    await s2.aclose()
    assert fakes[0].closed is False, "per-request aclose must not kill the shared client"
    await runtime.close_shared()
    assert fakes[0].closed is True, "app shutdown closes it"


async def test_embed_budget_reaches_the_embedder(env_only, monkeypatch) -> None:
    from src.services import embedding_service

    fake = FakeEmbedder()

    async def fake_load(settings=None):
        return {"provider": "openai_compatible", "api_key": "k", "base_url": "http://e", "model": "m", "dim": 4}

    monkeypatch.setattr(embedding_service, "load_embedding_config", fake_load)
    monkeypatch.setattr(embedding_service, "build_embedder_from", lambda c: fake)

    svc = embedding_service.EmbeddingService()
    await svc.embed(["q"], rate_limit_budget=QUERY_RATE_LIMIT_BUDGET)
    assert fake.last_budget == QUERY_RATE_LIMIT_BUDGET
    await svc.embed(["q"])
    assert fake.last_budget is None, "ingestion keeps the embedder default budget"


# ---------------------------------------------------------------------------
# M5 — the real embedder honours a per-call budget
# ---------------------------------------------------------------------------
async def test_openai_embedder_short_budget_fails_fast_on_429(monkeypatch) -> None:
    from src.embeddings.base import EmbeddingError
    from src.embeddings.openai_compatible_embedder import (
        OpenAICompatibleEmbedder,
        _RateLimited,
    )

    emb = OpenAICompatibleEmbedder(api_key="k", base_url="http://e", model="m")
    calls = {"n": 0}

    async def always_429(batch):
        calls["n"] += 1
        raise _RateLimited("429 slow down", None)

    monkeypatch.setattr(emb, "_call_once", always_429)
    with pytest.raises(EmbeddingError):
        await emb.embed(["text"], rate_limit_budget=0.0)
    assert calls["n"] == 1, "budget=0 must give up on the first 429, not wait 10 minutes"
    # Ingestion default is untouched.
    assert OpenAICompatibleEmbedder.RATE_LIMIT_BUDGET == 600.0


# ---------------------------------------------------------------------------
# Vector store cache
# ---------------------------------------------------------------------------
async def test_shared_vector_store_cached_and_dim_aware(env_only, monkeypatch) -> None:
    from src.vector_store import factory

    stores: List[FakeStore] = []
    monkeypatch.setattr(
        factory, "build_vector_store",
        lambda settings=None, **kw: (stores.append(FakeStore()), stores[-1])[1],
    )

    a = await factory.get_shared_vector_store()
    b = await factory.get_shared_vector_store()
    assert a is b and len(stores) == 1

    c = await factory.get_shared_vector_store(embedding_dimension_override=777)
    assert c is not a and len(stores) == 2, "a different effective dim must rebuild"


async def test_rag_retrieve_uses_query_budget_and_shared_store(env_only, monkeypatch) -> None:
    from src.services.rag_service import RAGService

    emb = FakeEmbedder()

    class EmbSvc:  # minimal stand-in matching the EmbeddingService surface
        async def embed(self, texts, *, rate_limit_budget=None):
            return await emb.embed(texts, rate_limit_budget=rate_limit_budget)

        async def aclose(self):
            pass

    store = FakeStore()
    rag = RAGService(vector_store=store, embedder=EmbSvc(), llm=object())
    await rag._retrieve("问题", {"top_k": 3, "similarity_threshold": 0.1})
    assert emb.last_budget == QUERY_RATE_LIMIT_BUDGET

    # Injected stores keep the old ownership semantics; shared ones survive.
    await rag.aclose()
    assert store.closed is True

    from src.vector_store import factory

    shared = FakeStore()

    async def fake_shared(settings=None, **kw):
        return shared

    monkeypatch.setattr(factory, "get_shared_vector_store", fake_shared)
    rag2 = RAGService(embedder=EmbSvc(), llm=object())
    await rag2.vector_store()
    await rag2.aclose()
    assert shared.closed is False, "the shared store belongs to the runtime cache"


# ---------------------------------------------------------------------------
# M6 — /process is queued: 202 now, progress via /list
# ---------------------------------------------------------------------------
@pytest.fixture
async def app_client(tmp_path: Path, monkeypatch):
    from src.main import create_app
    from src.services import auth_service

    db_file = tmp_path / "perf_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("APP_SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("APP_UPLOAD_DIR", str(tmp_path / "uploads"))
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


async def test_process_returns_202_and_finishes_in_background(app_client, monkeypatch) -> None:
    from src.api import documents as documents_api
    from src.services.document_service import DocumentService

    headers = await _token(app_client)
    r = await app_client.post(
        "/api/documents/upload",
        files={"file": ("note.txt", "Q：测试？\nA：回答\n".encode(), "text/plain")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    gate = asyncio.Event()

    async def fake_process(self, document_id, *, chunk_size=None, chunk_overlap=None,
                           on_started=None):
        async with session_scope() as s:
            d = await s.get(Document, document_id)
            d.status = "processing"
            d.chunks_total = 2
        if on_started is not None:
            on_started.set()
        await gate.wait()
        async with session_scope() as s:
            d = await s.get(Document, document_id)
            d.status = "ready"
            d.chunk_count = d.chunks_done = d.chunks_total = 2
        return {"id": document_id, "status": "ready", "chunk_count": 2}

    monkeypatch.setattr(DocumentService, "process_document", fake_process)

    r = await app_client.post(
        "/api/documents/process", json={"document_id": doc_id}, headers=headers
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "processing", "the queued response must report a truthful status"

    # Double submit while running → 400, not a second parallel ingest.
    r2 = await app_client.post(
        "/api/documents/process", json={"document_id": doc_id}, headers=headers
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "document_error"

    task = documents_api._ingest_tasks[doc_id]
    gate.set()
    await asyncio.wait_for(task, timeout=5)
    assert doc_id not in documents_api._ingest_tasks, "registry must self-clean"

    r3 = await app_client.get("/api/documents/list", headers=headers)
    row = next(d for d in r3.json() if d["id"] == doc_id)
    assert row["status"] == "ready" and row["chunk_count"] == 2


async def test_process_unknown_document_404s(app_client) -> None:
    headers = await _token(app_client)
    r = await app_client.post(
        "/api/documents/process", json={"document_id": "nope"}, headers=headers
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


async def test_process_ready_document_short_circuits(app_client, monkeypatch) -> None:
    """Re-submitting an already-ingested document returns its row, no re-embed."""
    from src.services.document_service import DocumentService

    headers = await _token(app_client)
    r = await app_client.post(
        "/api/documents/upload",
        files={"file": ("done.txt", b"content", "text/plain")},
        headers=headers,
    )
    doc_id = r.json()["id"]
    async with session_scope() as s:
        d = await s.get(Document, doc_id)
        d.status = "ready"
        d.chunk_count = 7

    calls = {"n": 0}
    real = DocumentService.process_document

    async def counting(self, document_id, **kw):
        calls["n"] += 1
        return await real(self, document_id, **kw)

    monkeypatch.setattr(DocumentService, "process_document", counting)
    r2 = await app_client.post(
        "/api/documents/process", json={"document_id": doc_id}, headers=headers
    )
    assert r2.status_code == 202
    assert r2.json()["status"] == "ready"
    assert calls["n"] == 1
