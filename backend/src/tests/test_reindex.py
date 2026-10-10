"""Vector-index rebuild: signature logic and the background rebuild job.

The job is exercised with a fake embedder (fixed-dim vectors), a fake vector
store (records reset()), and a faked per-document ingest that flips document
rows in the real throwaway SQLite DB — so the state machine, collection reset
and signature stamping run for real without chromadb or a vendor API.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.models.base import Base
from src.models.config import CSConfig
from src.models.document import Document
from src.services import reindex_service
from src.services.document_service import DocumentService
from src.services.embedding_service import embedding_signature
from src.services.reindex_service import (
    ReindexInProgress,
    needs_rebuild,
    signatures_equal,
    start_reindex,
    status_payload,
    wait_until_done,
)

PROBE_DIM = 384
EMB_CFG = {
    "provider": "openai_compatible",
    "api_key": "test-key",
    "base_url": "https://example.com/v1",
    "model": "fake-embed",
    "dim": 2048,  # intentionally wrong: the probe must override it
}
EXPECTED_SIG = {
    "provider": "openai_compatible",
    "model": "fake-embed",
    "base_url": "https://example.com/v1",
    "dim": PROBE_DIM,
}


# ---------------------------------------------------------------------------
# Pure logic — no DB
# ---------------------------------------------------------------------------
def test_signature_ignores_key_and_normalizes_url() -> None:
    a = embedding_signature({**EMB_CFG, "api_key": "key-one", "base_url": "https://e.com/v1/"})
    b = embedding_signature({**EMB_CFG, "api_key": "key-two", "base_url": "https://e.com/v1"})
    assert a == b
    assert "api_key" not in a


def test_signature_detects_space_changing_fields() -> None:
    base = embedding_signature(EMB_CFG)
    for field, value in (("model", "other-model"), ("base_url", "https://other/v1"), ("dim", 512)):
        changed = embedding_signature({**EMB_CFG, field: value})
        assert not signatures_equal(base, changed), field


def test_needs_rebuild_matrix() -> None:
    sig = embedding_signature(EMB_CFG)
    cur1024 = {**sig, "dim": 1024}
    assert needs_rebuild(None, sig, 0) is False            # nothing indexed yet
    assert needs_rebuild(None, sig, 3) is True             # pre-feature legacy index
    assert needs_rebuild(sig, sig, 3) is False             # in sync
    assert needs_rebuild(sig, {**sig, "dim": 512}, 3) is True
    # A live collection locked to another dimension says "rebuild" even with
    # zero ready documents (all docs failed/deleted but the lock survives).
    assert needs_rebuild(None, cur1024, 0, 2048) is True
    assert needs_rebuild(sig, cur1024, 0, 2048) is True
    # Same/unknown lock dimension and no docs → nothing to do yet.
    assert needs_rebuild(None, cur1024, 0, 1024) is False
    assert needs_rebuild(None, cur1024, 0, None) is False


# ---------------------------------------------------------------------------
# DB-backed job tests
# ---------------------------------------------------------------------------
class FakeEmbedder:
    def __init__(self, settings=None) -> None:
        pass

    async def embed(
        self, texts: List[str], *, rate_limit_budget: float | None = None
    ) -> List[List[float]]:
        return [[0.01] * PROBE_DIM for _ in texts]

    async def aclose(self) -> None:
        pass


class FakeVectorStore:
    """Stands in for a dimension-locked backend collection.

    ``locked_dim`` plays the role of Chroma's lazily-recorded dimension: it
    starts at the old model's 2048 and follows the factory's probed-dim
    override when the rebuild recreates the collection.
    """

    def __init__(self) -> None:
        self.resets = 0
        self.n = 0
        self.locked_dim = 2048

    async def reset(self) -> None:
        self.resets += 1
        self.n = 0

    async def count(self) -> int:
        return self.n

    async def dimension(self):
        return self.locked_dim

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def reindex_env(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "reindex_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    await dispose_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_store = FakeVectorStore()
    monkeypatch.setattr(reindex_service, "EmbeddingService", FakeEmbedder)

    def fake_factory(settings=None, *, embedding_dimension_override=None, **_kw):
        if embedding_dimension_override:
            fake_store.locked_dim = int(embedding_dimension_override)
        return fake_store

    monkeypatch.setattr(reindex_service, "build_vector_store", fake_factory)

    async def fake_shared(settings=None, *, embedding_dimension_override=None):
        # The diagnostics/ingest path (status_payload, open_vector_store). A
        # real dimension-locked backend reports the collection's ACTUAL dim
        # no matter what the client was built with — only reset() recreates
        # the collection, which fake_factory models. So: no mutation here.
        return fake_store

    monkeypatch.setattr(
        "src.services.document_service.get_shared_vector_store", fake_shared
    )

    # Per-test ingest script: filename -> ("ok" | "transient" | "fatal" | "hold")
    script: Dict[str, str] = {"mode": "ok"}
    attempts: Dict[str, int] = {}

    async def fake_process(
        self, document_id: str, chunk_size=None, chunk_overlap=None, on_started=None
    ):
        if on_started is not None:
            on_started.set()
        async with session_scope() as session:
            doc = await session.get(Document, document_id)
            mode = script.get(doc.filename, "ok")
            attempts[doc.filename] = attempts.get(doc.filename, 0) + 1
            if mode == "hold":
                await script["gate"].wait()
            if mode == "fatal":
                doc.status = "failed"
                doc.error_message = "解析失败：文件损坏"
                raise RuntimeError(doc.error_message)
            if mode == "transient" and attempts[doc.filename] == 1:
                doc.status = "failed"
                doc.error_message = "429 rate limit exceeded"
                raise RuntimeError("429 rate limit exceeded")
            doc.status = "ready"
            doc.chunk_count = doc.chunks_done = doc.chunks_total = 3
            doc.error_message = None
            return doc.to_dict()

    monkeypatch.setattr(DocumentService, "process_document", fake_process)

    # Fresh job state (and lock/task) for every event loop.
    reindex_service._STATE.clear()
    reindex_service._STATE.update(reindex_service._fresh_state())
    reindex_service._STATE_LOCK = asyncio.Lock()
    reindex_service._current_task = None

    async def add_config(signature=None, *, dim=2048):
        data: Dict[str, Any] = {
            "embedding": {**EMB_CFG, "dim": dim},
            "vector_db": {"provider": "chroma", "embedding_dimension": dim},
        }
        if signature is not None:
            data["index_signature"] = signature
        async with session_scope() as session:
            session.add(CSConfig(data=data))

    async def add_doc(name: str, *, status: str = "ready", file_exists: bool = True) -> str:
        f = tmp_path / f"{name}.txt"
        if file_exists:
            f.write_text("Q：测试？\nA：回答\n", encoding="utf-8")
        async with session_scope() as session:
            doc = Document(
                id=f"{name}-id",
                filename=name,
                file_path=str(f),
                file_size=10,
                file_md5=f"md5-{name}",
                parser_code="txt",
                status=status,
                chunk_count=3 if status == "ready" else 0,
                chunks_done=3 if status == "ready" else 0,
                chunks_total=3 if status == "ready" else 0,
            )
            session.add(doc)
        return f"{name}-id"

    yield type(
        "Env",
        (),
        {
            "store": fake_store,
            "script": script,
            "attempts": attempts,
            "add_config": staticmethod(add_config),
            "add_doc": staticmethod(add_doc),
        },
    )()

    await dispose_engine()
    get_settings.cache_clear()


async def _run_rebuild(**kw) -> Dict[str, Any]:
    await start_reindex(get_settings(), cooldown=0.01, max_rounds=20, **kw)
    return await wait_until_done(timeout=30)


async def test_full_rebuild_resets_collection_and_stamps_signature(reindex_env) -> None:
    await reindex_env.add_config(signature=None)
    await reindex_env.add_doc("a")
    await reindex_env.add_doc("b")

    state = await _run_rebuild()

    assert state["phase"] == "done"
    assert reindex_env.store.resets == 1
    assert reindex_env.attempts == {"a": 1, "b": 1}

    async with session_scope() as session:
        row = (await session.execute(select(CSConfig).limit(1))).scalar_one()
    assert row.data["index_signature"] == EXPECTED_SIG
    assert row.data["vector_db"]["embedding_dimension"] == PROBE_DIM
    assert row.data["embedding"]["dim"] == PROBE_DIM

    status = await status_payload(get_settings())
    assert status["needs_rebuild"] is False
    assert status["docs_ready"] == 2


async def test_repair_mode_skips_reset_and_only_reprocesses_unfinished(reindex_env) -> None:
    await reindex_env.add_config(signature=EXPECTED_SIG)
    await reindex_env.add_doc("done", status="ready")
    await reindex_env.add_doc("todo", status="failed")

    state = await _run_rebuild()

    assert state["phase"] == "done"
    assert state["repair_mode"] is True
    assert reindex_env.store.resets == 0
    assert reindex_env.attempts == {"todo": 1}


async def test_transient_error_retried_with_backoff(reindex_env) -> None:
    await reindex_env.add_config(signature=EXPECTED_SIG)
    reindex_env.script["flaky"] = "transient"
    await reindex_env.add_doc("flaky", status="failed")

    state = await _run_rebuild()

    assert state["phase"] == "done"
    assert reindex_env.attempts["flaky"] == 2


async def test_permanent_error_fails_fast_and_keeps_signature(reindex_env) -> None:
    await reindex_env.add_config(signature=None)
    reindex_env.script["bad"] = "fatal"
    await reindex_env.add_doc("bad")

    state = await _run_rebuild()

    assert state["phase"] == "failed"
    assert state["failed"][0]["filename"] == "bad"
    assert reindex_env.attempts["bad"] == 1          # no 100-round wait on deterministic error
    # Signature stamped before indexing: a retry runs in repair mode instead
    # of wiping the vectors that did embed successfully.
    async with session_scope() as session:
        row = (await session.execute(select(CSConfig).limit(1))).scalar_one()
    assert row.data["index_signature"] == EXPECTED_SIG


async def test_missing_source_file_reported_without_processing(reindex_env) -> None:
    await reindex_env.add_config(signature=None)
    await reindex_env.add_doc("ghost", file_exists=False)

    state = await _run_rebuild()

    assert state["phase"] == "failed"
    assert "原文件已丢失" in state["failed"][0]["error"]
    assert "ghost" not in reindex_env.attempts


async def test_dimension_lock_triggers_rebuild_without_ready_docs(reindex_env) -> None:
    """0 ready docs but the on-disk collection is locked to the old dimension:
    this is exactly 'I deleted everything and uploads still fail'."""
    await reindex_env.add_config(signature=None, dim=1024)
    await reindex_env.add_doc("stuck", status="failed")
    reindex_env.store.locked_dim = 2048

    before = await status_payload(get_settings())
    assert before["store_dimension"] == 2048
    assert before["needs_rebuild"] is True

    state = await _run_rebuild()
    assert state["phase"] == "done"
    assert reindex_env.store.resets == 1
    assert reindex_env.attempts == {"stuck": 1}

    after = await status_payload(get_settings())
    assert after["store_dimension"] == PROBE_DIM
    assert after["docs_ready"] == 1
    assert after["needs_rebuild"] is False


async def test_second_start_while_running_is_rejected(reindex_env) -> None:
    gate = asyncio.Event()
    reindex_env.script["gate"] = gate
    reindex_env.script["slow"] = "hold"
    await reindex_env.add_config(signature=EXPECTED_SIG)
    await reindex_env.add_doc("slow", status="failed")

    await start_reindex(get_settings(), cooldown=0.01)
    # Wait until the task is actually inside the held ingest.
    for _ in range(100):
        if reindex_service._snapshot()["current_file"] == "slow":
            break
        await asyncio.sleep(0.02)
    assert reindex_service._snapshot()["current_file"] == "slow"
    with pytest.raises(ReindexInProgress):
        await start_reindex(get_settings())

    gate.set()
    state = await wait_until_done(timeout=10)
    assert state["phase"] == "done"
