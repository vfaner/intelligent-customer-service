"""Verify Python + dependencies + config + optional services.

Exits non-zero on any required-missing problem so it can be wired into CI.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

REQUIRED = [
    ("fastapi",        "FastAPI"),
    ("uvicorn",        "Uvicorn"),
    ("pydantic",       "Pydantic"),
    ("pydantic_settings", "Pydantic Settings"),
    ("httpx",          "HTTPX"),
    ("sqlalchemy",     "SQLAlchemy"),
    ("openpyxl",       "openpyxl (xlsx)"),
    ("docx",           "python-docx"),
]

OPTIONAL = [
    ("chromadb",       "ChromaDB"),
    ("qdrant_client",  "Qdrant client"),
    ("pymilvus",       "Milvus client (pymilvus)"),
    ("asyncpg",        "asyncpg (PostgreSQL 及 PG 协议信创库)"),
    ("dmAsync",        "dmAsync (达梦 DM8 异步驱动，信创可选)"),
    ("milvus_lite",    "Milvus Lite (本地 .db 模式需要)"),
    ("PyPDF2",         "PyPDF2 (pdf support)"),
    ("tenacity",       "tenacity"),
]


def _check(group: list[tuple[str, str]]) -> int:
    failed = 0
    for module, label in group:
        try:
            importlib.import_module(module)
            logger.info("  ✓ {}", label)
        except Exception as e:
            failed += 1
            logger.error("  ✗ {} — {}", label, e)
    return failed


def main() -> int:
    logger.info("── Required ──")
    req_failures = _check(REQUIRED)
    logger.info("── Optional ──")
    _check(OPTIONAL)

    logger.info("── Configuration ──")
    from src.core.config import get_settings
    s = get_settings()
    logger.info("  APP_ENV        = {}", s.app.env)
    logger.info("  DATABASE_URL   = {}", s.database.url)
    # 按 DATABASE_URL 实际用到的 scheme 探驱动，缺了直接打出安装命令。
    db_driver_failures = 0
    try:
        from src.core.db_drivers import ensure_driver
        ensure_driver(s.database.url)
        logger.info("  ✓ DATABASE_URL 驱动已就绪")
    except Exception as e:
        db_driver_failures = 1
        for line in str(e).splitlines():
            logger.error("  ✗ {}", line)
    logger.info("  VECTOR_DB      = {}  collection={}", s.vector_db.provider, s.vector_db.collection)
    logger.info("  LLM provider   = {}  model={}  protocol={}", s.llm.provider, s.llm.model, s.llm.protocol)
    logger.info("  Embedding      = {}  model={}  dim={}", s.embedding.provider, s.embedding.model, s.embedding.dim)
    logger.info("  RAG            = top_k={}  threshold={}  chunk={}/{}",
                s.rag.top_k, s.rag.similarity_threshold, s.rag.chunk_size, s.rag.chunk_overlap)

    if not s.llm.api_key:
        logger.warning("  ⚠ LLM_API_KEY is empty — set it in .env before using chat endpoints.")
    if not s.embedding.api_key:
        logger.warning("  ⚠ EMBEDDING_API_KEY is empty — set it in .env before ingesting documents.")

    if req_failures:
        logger.error("❌ {} required package(s) missing. Run: pip install -r requirements.txt", req_failures)
        return 1
    if db_driver_failures:
        logger.error("❌ DATABASE_URL 所需驱动缺失，按上面的 pip 命令安装后重试。")
        return 1
    logger.info("✅ Environment looks good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
