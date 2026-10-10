"""Model listing + connection-test API (note: file is named models.py but exposes /api/models)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.registry import (
    get_provider,
    list_providers,
    list_vector_db_providers,
)

router = APIRouter()


class TestIn(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    protocol: Optional[str] = None


@router.get("", summary="List supported LLM providers + vector DB providers")
async def list_models() -> dict:
    return {
        "providers": list_providers(),
        "vector_db_providers": list_vector_db_providers(),
    }


@router.get("/vector-db", summary="List supported vector DB providers")
async def list_vector_dbs() -> dict:
    return {"providers": list_vector_db_providers()}


@router.post("/test", summary="Test the active or overridden LLM connection")
async def test_model(body: TestIn) -> dict:
    """Run a one-shot prompt and return success/failure + reply snippet.

    If any field in the body is omitted, the active ``CSConfig`` (or .env)
    value is used.
    """
    override: dict = {}
    if body.provider:
        spec = get_provider(body.provider)
        if spec is not None:
            override.setdefault("model_base_url", spec.base_url)
            override.setdefault("protocol", spec.protocol)
    if body.model:
        override["model_name"] = body.model
    if body.api_key:
        override["model_api_key"] = body.api_key
    if body.base_url:
        override["model_base_url"] = body.base_url
    if body.protocol:
        override["protocol"] = body.protocol

    from ..adapters.factory import build_provider
    from ..core.config import get_settings

    s = get_settings()
    from ..utils.obfuscation import deobfuscate
    from ..core.database import session_scope
    from ..models.config import fetch_config_row

    async with session_scope() as session:
        row = await fetch_config_row(session)
        runtime = row.data if row else {}

    api_key = override.get("model_api_key") or deobfuscate(runtime.get("model_api_key") or "") or s.llm.api_key
    base_url = override.get("model_base_url") or runtime.get("model_base_url") or s.llm.base_url
    model = override.get("model_name") or runtime.get("model_name") or s.llm.model
    protocol = (override.get("protocol") or runtime.get("protocol") or s.llm.protocol or "openai").lower()

    provider = build_provider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        protocol=protocol,
        settings=s,
    )
    try:
        # BaseProvider.test() is the single copy of this probe (it used to be
        # duplicated here verbatim, drift included).
        ok, message = await provider.test()
        return {"ok": ok, "message": message}
    finally:
        if hasattr(provider, "aclose"):
            await provider.aclose()  # type: ignore[attr-defined]


class EmbeddingTestIn(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


@router.post("/test-embedding", summary="Test the embedding model connection")
async def test_embedding(body: EmbeddingTestIn) -> dict:
    """Embed a short probe string and report the vector dimension.

    Any omitted field falls back to the saved config (DB, then .env). The
    returned ``dim`` is what the vector DB dimension must be set to.
    """
    from ..services.embedding_service import build_embedder_from, load_embedding_config

    cfg = await load_embedding_config()
    if body.api_key and not body.api_key.startswith("****"):
        cfg["api_key"] = body.api_key
    if body.base_url:
        cfg["base_url"] = body.base_url
    if body.model:
        cfg["model"] = body.model

    embedder = None
    try:
        embedder = build_embedder_from(cfg)
        vectors = await embedder.embed(["连接测试"])
        if not vectors or not vectors[0]:
            return {"ok": False, "message": "测试失败: 接口返回了空向量"}
        dim = len(vectors[0])
        configured = int(cfg.get("dim") or 0)
        msg = f"测试成功 - 模型 {cfg['model']} 返回 {dim} 维向量"
        if configured and configured != dim:
            msg += f"（⚠ 当前配置维度是 {configured}，请改成 {dim} 并重新入库文档）"
        return {"ok": True, "message": msg, "dim": dim, "configured_dim": configured}
    except Exception as e:
        return {"ok": False, "message": f"测试失败: {e}"}
    finally:
        if embedder is not None and hasattr(embedder, "aclose"):
            await embedder.aclose()  # type: ignore[attr-defined]


class AvailableModelsIn(BaseModel):
    provider: Optional[str] = None
    protocol: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None


@router.post("/available", summary="List the models a vendor actually offers")
async def available_models(body: AvailableModelsIn) -> dict:
    """Query the vendor's ``GET {base}/models`` endpoint.

    Falls back to the curated ``recommended_models`` list when the vendor
    doesn't implement listing, or when the call fails (bad key, no network,
    Anthropic-only endpoint, …). ``source`` tells the UI which happened so
    it can label the dropdown honestly.
    """
    import httpx

    from ..core.config import get_settings
    from ..core.database import session_scope
    from ..core.registry import get_provider
    from ..models.config import fetch_config_row
    from ..utils.obfuscation import deobfuscate

    settings = get_settings()
    async with session_scope() as session:
        row = await fetch_config_row(session)
        runtime = (row.data or {}) if row else {}

    provider_code = body.provider or runtime.get("model_provider") or settings.llm.provider
    spec = get_provider(provider_code)
    protocol = (body.protocol or runtime.get("protocol") or settings.llm.protocol or "openai").lower()

    endpoint = spec.endpoint_for(protocol) if spec else None
    base_url = (
        body.base_url
        or (endpoint.base_url if endpoint else "")
        or runtime.get("model_base_url")
        or settings.llm.base_url
    )
    api_key = body.api_key
    if not api_key or api_key.startswith("****"):
        api_key = deobfuscate(runtime.get("model_api_key") or "") or settings.llm.api_key

    recommended = list(spec.recommended_models) if spec else []

    def fallback(reason: str) -> dict:
        return {
            "ok": bool(recommended),
            "models": recommended,
            "source": "recommended",
            "message": reason,
        }

    if spec is not None and not spec.supports_model_listing:
        return fallback("该厂商不支持列出模型，以下是内置推荐列表")
    if not base_url:
        return fallback("未填写 Base URL，无法查询；以下是内置推荐列表")
    if not api_key:
        return fallback("未配置 API Key，无法查询；以下是内置推荐列表")

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    if protocol == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            return fallback(f"厂商接口返回 HTTP {resp.status_code}，改用内置推荐列表")
        payload = resp.json()
    except Exception as e:
        return fallback(f"查询失败（{type(e).__name__}），改用内置推荐列表")

    # OpenAI shape: {"data": [{"id": "..."}]} · Anthropic: {"data": [{"id": ...}]}
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        items = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return fallback("厂商返回格式无法解析，改用内置推荐列表")

    ids: list[str] = []
    for it in items:
        mid = it.get("id") or it.get("name") if isinstance(it, dict) else (it if isinstance(it, str) else None)
        if mid:
            ids.append(str(mid))

    if not ids:
        return fallback("厂商返回了空列表，改用内置推荐列表")

    # Chat models first (drop obvious embedding / tts / image entries), then A-Z.
    noise = ("embed", "tts", "whisper", "dall-e", "moderation", "rerank", "audio", "image")
    chat = sorted(m for m in ids if not any(n in m.lower() for n in noise))
    other = sorted(m for m in ids if any(n in m.lower() for n in noise))
    return {
        "ok": True,
        "models": chat + other,
        "source": "api",
        "message": f"已从厂商接口获取 {len(ids)} 个模型",
    }
