"""Embedding service — resolves config from CSConfig (DB) then falls back to .env.

Reading the DB row on each build means a config change made in the Admin UI
takes effect immediately, matching the behaviour of ``LLMService``.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from ..core.config import Settings, get_settings
from ..core.exceptions import EmbeddingError as _EmbeddingAppError
from ..embeddings.base import BaseEmbedder
from ..embeddings.openai_compatible_embedder import OpenAICompatibleEmbedder
from ..models.config import CSConfig
from ..utils.obfuscation import deobfuscate
from . import runtime

log = logging.getLogger(__name__)


async def load_embedding_config(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Merge the DB config over the .env defaults.

    Fields are merged per-key, which can pair a key saved in the admin console
    with a base_url that came from .env — i.e. one vendor's credential sent to
    another vendor's endpoint, which fails as a confusing 401. ``_mixed_sources``
    records where each field came from so callers can warn about that.
    """
    settings = settings or get_settings()
    e = settings.embedding
    resolved = {
        "provider": e.provider,
        "api_key": e.api_key,
        "base_url": e.base_url,
        "model": e.model,
        "dim": e.dim,
    }
    origin = {k: ("env" if v else "unset") for k, v in resolved.items()}

    try:
        from ..core.database import session_scope

        async with session_scope() as session:
            row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
            runtime = (row.data or {}).get("embedding") if row else None
    except Exception as exc:  # DB not ready yet — .env only
        log.debug("Could not read embedding config from DB: %s", exc)
        runtime = None

    if runtime:
        if runtime.get("provider"):
            resolved["provider"] = runtime["provider"]
            origin["provider"] = "db"
        stored_key = deobfuscate(runtime.get("api_key") or "")
        if stored_key:
            resolved["api_key"] = stored_key
            origin["api_key"] = "db"
        if runtime.get("base_url"):
            resolved["base_url"] = runtime["base_url"]
            origin["base_url"] = "db"
        if runtime.get("model"):
            resolved["model"] = runtime["model"]
            origin["model"] = "db"
        if runtime.get("dim"):
            resolved["dim"] = int(runtime["dim"])
            origin["dim"] = "db"

    resolved["_origin"] = origin
    return resolved


_WHERE_TO_FIX = "请在管理后台「模型配置 → 🧬 向量模型」中填写，填完点「🔌 测试并探测维度」验证。"

# 429 retry budget for the INTERACTIVE path (embedding a chat question). The
# embedder's own default (600s) is tuned for background ingestion, where
# riding out an exhausted vendor quota beats failing; a waiting human must
# get an actionable error in seconds instead.
QUERY_RATE_LIMIT_BUDGET = 15.0


def embedding_signature(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Identity of the embedding space a vector index was built with.

    Stored on CSConfig as ``index_signature`` after a (re)build. A difference
    between this and the currently configured model means the stored vectors
    are incompatible — same-dim/different-model swaps are undetectable at
    insert time but retrieval silently turns to noise, so the model name and
    base URL matter, not just the dimension.

    The API key is deliberately excluded: rotating a key keeps the same
    embedding space and must not force a rebuild.
    """
    return {
        "provider": str(cfg.get("provider") or "openai_compatible"),
        "model": str(cfg.get("model") or ""),
        "base_url": str(cfg.get("base_url") or "").rstrip("/"),
        "dim": int(cfg.get("dim") or 0),
    }


def build_embedder_from(cfg: Dict[str, Any]) -> BaseEmbedder:
    provider = cfg.get("provider") or "openai_compatible"
    if provider != "openai_compatible":
        raise _EmbeddingAppError(f"不支持的向量模型类型: {provider}")

    # Report every missing field at once — fixing them one 401 at a time is
    # miserable.
    missing = [
        label
        for key, label in (("base_url", "Base URL"), ("model", "模型名称"), ("api_key", "API Key"))
        if not cfg.get(key)
    ]
    if missing:
        raise _EmbeddingAppError(
            f"向量模型配置不完整，缺少：{'、'.join(missing)}。{_WHERE_TO_FIX}",
            details={
                "provider": provider,
                "missing": missing,
                "base_url": cfg.get("base_url", ""),
                "model": cfg.get("model", ""),
                "api_key_set": bool(cfg.get("api_key")),
            },
        )

    # 防呆：DeepSeek 没有向量模型（embedding）接口。聊天模型用 DeepSeek 的人
    # 常把同一个 base_url 抄到这里，之后每次调用都得到裸 404，很难看出原因。
    # 放在混合来源检查之前——URL 本身就不可能工作时，来源讨论没有意义。
    if "deepseek.com" in str(cfg.get("base_url") or "").lower():
        raise _EmbeddingAppError(
            "DeepSeek 不提供向量化（embedding）接口，这个 Base URL 调用只会返回 404。"
            "请换成提供 embedding 的厂商（OpenAI text-embedding-3-small、"
            "阿里百炼 text-embedding-v3、智谱 embedding-3、硅基流动 BAAI/bge-m3 等），"
            f"它可以和聊天模型不是同一家。{_WHERE_TO_FIX}",
            details={
                "base_url": cfg["base_url"],
                "reason": "deepseek_has_no_embedding_api",
            },
        )

    # Catch the silent trap: a key you saved in the console being sent to a
    # base_url that defaulted from .env (or vice versa). That's almost always
    # two different vendors and surfaces as an opaque 401.
    origin = cfg.get("_origin") or {}
    if origin.get("api_key") == "db" and origin.get("base_url") == "env":
        raise _EmbeddingAppError(
            "向量模型的 API Key 来自管理后台，但 Base URL 用的是 .env 里的默认值 "
            f"（{cfg['base_url']}）——两者很可能不是同一家厂商，会导致 401。"
            f"{_WHERE_TO_FIX}",
            details={
                "base_url": cfg["base_url"],
                "base_url_from": "env",
                "api_key_from": "db",
                "model": cfg.get("model", ""),
            },
        )

    return OpenAICompatibleEmbedder(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        dim=int(cfg.get("dim") or 0),
    )


class EmbeddingService:
    """Resolves the embedder from DB config (falling back to .env) and shares it.

    The embedder — and its httpx connection pool — is a process-wide singleton
    keyed by the resolved config (see ``services.runtime``). Console edits
    still take effect immediately: any change to the resolved fields builds a
    fresh embedder on the next call, and the old one is retired.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings
        self._embedder: BaseEmbedder | None = None

    @staticmethod
    def _fingerprint(cfg: Dict[str, Any]) -> str:
        # The API key enters the fingerprint hashed — the string stays in
        # memory only, but there's no reason to keep a raw credential in it.
        key_hash = hashlib.sha256(
            str(cfg.get("api_key") or "").encode("utf-8")
        ).hexdigest()[:16]
        ident = "|".join(
            str(cfg.get(k) or "") for k in ("provider", "base_url", "model", "dim")
        )
        return f"{ident}|{key_hash}"

    async def get(self) -> BaseEmbedder:
        cfg = await load_embedding_config(self._settings)
        fp = self._fingerprint(cfg)

        async def _build() -> BaseEmbedder:
            return build_embedder_from(cfg)

        self._embedder = await runtime.get_shared("embedder", fp, _build)
        return self._embedder

    async def embed(
        self, texts: List[str], *, rate_limit_budget: Optional[float] = None
    ) -> List[List[float]]:
        embedder = await self.get()
        return await embedder.embed(texts, rate_limit_budget=rate_limit_budget)

    async def aclose(self) -> None:
        """Release this service's reference; the shared embedder stays open.

        Closing it here would defeat the connection reuse and could cut a
        concurrent request's call mid-flight. ``runtime.close_shared()``
        closes it at app shutdown instead.
        """
        self._embedder = None
