"""Admin utility endpoints: status check + one-click initialization.

These power the "first-run wizard" in the Admin UI: a fresh install can be
bootstrapped entirely from the browser without touching the CLI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import inspect, select, text

from ..core.config import get_settings
from ..core.database import get_engine, session_scope
from ..core.registry import AI_PROVIDERS, get_provider
from ..models.base import Base
from ..models.config import CSConfig
from ..models.document import Document

# Make sure all mappers are registered on Base.metadata.
import src.models  # noqa: F401

log = logging.getLogger(__name__)
router = APIRouter()


# ----- Tables we expect to be present for a healthy install ----------
EXPECTED_TABLES = {
    "cs_config",
    "cs_session",
    "cs_message",
    "cs_document",
    "cs_admin_user",
}


async def _check_db() -> bool:
    """True if every expected table exists."""
    try:
        engine = get_engine()
        def _tables(conn):
            insp = inspect(conn)
            return set(insp.get_table_names())
        async with engine.connect() as conn:
            existing = await conn.run_sync(_tables)
        missing = EXPECTED_TABLES - existing
        if missing:
            log.info("DB not ready, missing tables: %s", missing)
            return False
        return True
    except Exception as e:
        log.warning("DB check failed: %s", e)
        return False


async def _has_config_row() -> bool:
    try:
        async with session_scope() as session:
            row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
            return row is not None
    except Exception as e:
        log.warning("config check failed: %s", e)
        return False


async def _has_documents() -> bool:
    try:
        async with session_scope() as session:
            n = (await session.execute(select(Document.id).limit(1))).scalar_one_or_none()
            return n is not None
    except Exception as e:
        log.warning("doc check failed: %s", e)
        return False


# ----- Endpoints ------------------------------------------------------
@router.get("/status", summary="Check installation status")
async def status() -> Dict[str, Any]:
    """Inspect the system and return what's missing for a healthy install.

    The Admin UI calls this on load to decide whether to show the
    "first-run wizard" banner.
    """
    settings = get_settings()
    db_ready = await _check_db()
    has_config = await _has_config_row() if db_ready else False
    has_documents = await _has_documents() if db_ready else False
    upload_dir = Path(settings.app.upload_dir)
    chroma_dir = Path(settings.vector_db.chroma_persist_dir)

    issues: List[str] = []
    fixes: List[str] = []

    if not db_ready:
        issues.append("数据库表未创建")
        fixes.append("create_tables")
    if db_ready and not has_config:
        issues.append("客服配置未初始化")
        fixes.append("seed_config")
    if not upload_dir.exists():
        issues.append(f"上传目录不存在: {upload_dir}")
    if settings.vector_db.provider == "chroma" and not chroma_dir.exists():
        issues.append(f"Chroma 持久化目录不存在: {chroma_dir}")

    # API keys can live in .env *or* in the CSConfig row (set via Admin UI).
    llm_key_set, embedding_key_set = await _api_keys_set(settings)

    return {
        "ready": not issues,
        "issues": issues,
        "fixes": fixes,
        "details": {
            "db_ready": db_ready,
            "has_config": has_config,
            "has_documents": has_documents,
            "doc_count": (await _doc_count()) if db_ready else 0,
            "upload_dir": str(upload_dir),
            "upload_dir_exists": upload_dir.exists(),
            "chroma_dir": str(chroma_dir),
            "chroma_dir_exists": chroma_dir.exists(),
            "vector_db_provider": settings.vector_db.provider,
            "llm_provider": settings.llm.provider,
            "llm_api_key_set": llm_key_set,
            "embedding_api_key_set": embedding_key_set,
            # Drives the console's "you're still on admin/123456" reminder.
            "default_password_in_use": (await _default_password_in_use()) if db_ready else False,
        },
    }


async def _default_password_in_use() -> bool:
    """True when the seed admin still has the factory password."""
    if not get_settings().auth.enabled:
        return False
    try:
        async with session_scope() as session:
            from ..services.auth_service import is_default_password_in_use

            return await is_default_password_in_use(session)
    except Exception as e:
        log.debug("default password check failed: %s", e)
        return False


async def _api_keys_set(settings) -> tuple[bool, bool]:
    """Report whether the LLM / embedding keys are configured anywhere."""
    from ..utils.obfuscation import deobfuscate

    llm_set = bool(settings.llm.api_key)
    emb_set = bool(settings.embedding.api_key)
    try:
        async with session_scope() as session:
            row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
            data = (row.data or {}) if row else {}
        if deobfuscate(data.get("model_api_key") or ""):
            llm_set = True
        emb = data.get("embedding") or {}
        if deobfuscate(emb.get("api_key") or ""):
            emb_set = True
    except Exception as e:
        log.debug("key check via DB failed: %s", e)
    return llm_set, emb_set


async def _doc_count() -> int:
    try:
        async with session_scope() as session:
            from sqlalchemy import func
            res = await session.execute(select(func.count(Document.id)))
            return int(res.scalar() or 0)
    except Exception:
        return 0


class InitResult(BaseModel):
    ok: bool
    message: str
    actions: List[str] = []
    details: Dict[str, Any] = {}


# Columns added after the initial schema shipped. SQLAlchemy's create_all()
# only creates missing *tables*, so upgrading an existing install needs this.
_EXPECTED_COLUMNS: Dict[str, Dict[str, str]] = {
    "cs_document": {
        "chunks_done": "INTEGER NOT NULL DEFAULT 0",
        "chunks_total": "INTEGER NOT NULL DEFAULT 0",
    },
}


async def _add_missing_columns(engine) -> List[str]:
    """Add any known-missing columns to existing tables. Returns what it added."""
    added: List[str] = []

    def _existing(conn, table: str) -> set[str]:
        insp = inspect(conn)
        if table not in insp.get_table_names():
            return set()
        return {c["name"] for c in insp.get_columns(table)}

    for table, columns in _EXPECTED_COLUMNS.items():
        try:
            async with engine.connect() as conn:
                have = await conn.run_sync(_existing, table)
            if not have:
                continue      # table doesn't exist yet; create_all handled it
            for col, ddl in columns.items():
                if col in have:
                    continue
                async with engine.begin() as conn:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added.append(f"{table}.{col}")
                log.info("Added missing column %s.%s", table, col)
        except Exception as e:
            log.warning("Could not add columns to %s: %s", table, e)
    return added


@router.post("/init", response_model=InitResult, summary="Run one-click initialization")
async def init_all() -> InitResult:
    """Create tables, seed default config, prepare runtime directories.

    Safe to call multiple times — all steps are idempotent.
    """
    settings = get_settings()
    actions: List[str] = []
    details: Dict[str, Any] = {}

    # 1) Ensure runtime directories exist
    upload_dir = Path(settings.app.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    actions.append(f"创建上传目录: {upload_dir}")
    details["upload_dir"] = str(upload_dir)

    chroma_dir = Path(settings.vector_db.chroma_persist_dir)
    chroma_dir.mkdir(parents=True, exist_ok=True)
    actions.append(f"创建 Chroma 目录: {chroma_dir}")
    details["chroma_dir"] = str(chroma_dir)

    # 2) Create all tables
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    actions.append("创建数据库表 (cs_config / cs_session / cs_message / cs_document / cs_admin_user)")
    details["tables_created"] = True

    # 2b) Add columns introduced after a table was first created.
    #     `create_all` never alters existing tables, so upgrading an older
    #     install would otherwise crash on the new progress columns.
    added = await _add_missing_columns(engine)
    if added:
        actions.append(f"补充新增字段：{'、'.join(added)}")
    details["columns_added"] = added

    # 3) Seed the admin login account if the user table is empty
    async with session_scope() as session:
        from ..services.auth_service import ensure_default_admin

        seeded = await ensure_default_admin(session)
    if seeded is not None:
        actions.append(
            f"创建管理员账号（{settings.auth.default_username} / "
            f"{settings.auth.default_password}，请尽快修改密码）"
        )
    else:
        actions.append("管理员账号已存在，跳过")
    details["seeded_admin_user"] = seeded is not None

    # 4) Seed default CSConfig if missing
    async with session_scope() as session:
        existing = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
        if existing is None:
            provider = get_provider(settings.llm.provider) or AI_PROVIDERS[0]
            base_url = settings.llm.base_url or provider.base_url
            model = settings.llm.model or provider.default_model
            protocol = settings.llm.protocol

            data = {
                "name": "智能客服小助手",
                "avatar": "",
                "contact_phone": "",
                "contact_email": "",
                "welcome_message": "您好，请问有什么可以帮您？",
                "model_provider": provider.code,
                "model_name": model,
                "model_api_key": settings.llm.api_key,
                "model_base_url": base_url,
                "protocol": protocol,
                "temperature": settings.llm.temperature,
                "max_tokens": settings.llm.max_tokens,
                "embedding": {
                    "provider": settings.embedding.provider,
                    "api_key": settings.embedding.api_key,
                    "base_url": settings.embedding.base_url or provider.base_url,
                    "model": settings.embedding.model or provider.default_embedding_model,
                    "dim": settings.embedding.dim,
                },
                "vector_db": {
                    "provider": settings.vector_db.provider,
                    "host": settings.vector_db.host,
                    "port": settings.vector_db.port,
                    "collection": settings.vector_db.collection,
                    "embedding_dimension": settings.vector_db.embedding_dim,
                    "persist_directory": settings.vector_db.chroma_persist_dir,
                    "milvus_uri": settings.vector_db.milvus_uri,
                    "milvus_token": settings.vector_db.milvus_token,
                    "milvus_db_name": settings.vector_db.milvus_db_name,
                },
                "rag": {
                    "top_k": settings.rag.top_k,
                    "similarity_threshold": settings.rag.similarity_threshold,
                    "chunk_size": settings.rag.chunk_size,
                    "chunk_overlap": settings.rag.chunk_overlap,
                },
                "active_provider_code": provider.code,
            }
            session.add(CSConfig(data=data))
            actions.append(f"写入默认客服配置（provider: {provider.code}, model: {model}）")
            details["seeded_config"] = True
        else:
            actions.append("客服配置已存在，跳过")
            details["seeded_config"] = False

    return InitResult(
        ok=True,
        message="初始化完成",
        actions=actions,
        details=details,
    )


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------
def _parse_version(v: str) -> tuple:
    """Turn 'v1.2.3' / '1.2' into a comparable tuple, ignoring a leading 'v'.

    Non-numeric parts sort as 0 so a malformed tag can't crash the comparison.
    """
    cleaned = (v or "").strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for chunk in cleaned.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    # Pad so 1.2 and 1.2.0 compare equal
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


@router.get("/version", summary="Check whether a newer release exists on GitHub")
async def version_check(refresh: bool = False) -> Dict[str, Any]:
    """Compare the running version against the repo's latest release/tag.

    Deliberately read-only: it reports what's available and how to update, but
    never touches the working tree. Auto-pulling would overwrite uncommitted
    local changes and could leave the service unable to start — not something
    to trigger from a button click.

    Never raises: any network/parse problem comes back as ok=false with a
    message, so the UI can show it inline instead of breaking.
    """
    import httpx

    settings = get_settings()
    current = settings.app.version
    repo = (settings.app.github_repo or "").strip().strip("/")

    base: Dict[str, Any] = {
        "ok": False,
        "current": current,
        "latest": None,
        "has_update": False,
        "repo": repo,
        "release_url": None,
        "notes": None,
        "message": "",
    }

    if not repo:
        base["message"] = (
            "未配置仓库地址。在 .env 里设置 APP_GITHUB_REPO=owner/repo 后即可检查更新。"
        )
        base["configured"] = False
        return base
    base["configured"] = True

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ics-update-check"}
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            # Prefer releases; fall back to tags for repos that only tag.
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/releases/latest", headers=headers
            )
            latest = notes = release_url = None
            if resp.status_code == 200:
                data = resp.json()
                latest = data.get("tag_name") or data.get("name")
                notes = (data.get("body") or "").strip()[:1500] or None
                release_url = data.get("html_url")
            elif resp.status_code == 404:
                # Releases 404 both when the repo has none and when the repo
                # itself is wrong — check tags to tell the two apart, so a typo
                # in APP_GITHUB_REPO doesn't look like "no releases yet".
                tags = await client.get(
                    f"https://api.github.com/repos/{repo}/tags?per_page=1", headers=headers
                )
                if tags.status_code == 404:
                    base["message"] = (
                        f"找不到仓库 {repo}。请检查 APP_GITHUB_REPO 是否写成 owner/repo 形式，"
                        f"以及仓库是否为私有。"
                    )
                    return base
                if tags.status_code == 200 and tags.json():
                    latest = tags.json()[0].get("name")
                    release_url = f"https://github.com/{repo}/tags"
                else:
                    base["message"] = "仓库还没有发布任何 Release 或 Tag，无法比较版本。"
                    return base
            elif resp.status_code == 403:
                base["message"] = "GitHub API 限流（未认证时每小时 60 次），请稍后再试。"
                return base
            else:
                base["message"] = f"查询 GitHub 失败：HTTP {resp.status_code}"
                return base
    except Exception as e:
        log.info("update check failed: %s", e)
        base["message"] = f"无法连接 GitHub（{type(e).__name__}），请检查服务器网络。"
        return base

    if not latest:
        base["message"] = "GitHub 返回的数据里没有版本号。"
        return base

    has_update = _parse_version(latest) > _parse_version(current)
    base.update(
        ok=True,
        latest=latest,
        has_update=has_update,
        release_url=release_url or f"https://github.com/{repo}/releases",
        notes=notes,
        message=(
            f"发现新版本 {latest}（当前 {current}）"
            if has_update
            else f"已是最新版本（{current}）"
        ),
    )
    return base
