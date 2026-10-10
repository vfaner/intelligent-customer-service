"""LLM service: a thin façade over the active provider.

Resolves the provider config on every call so an update via ``PUT /api/config``
takes effect immediately without restarting the server — but the provider
*object* (and its httpx connection pool) is shared process-wide and only
rebuilt when the resolved config actually changes (see services/runtime).
Before that, every chat message paid a full TCP+TLS handshake to the vendor.
"""
from __future__ import annotations

import hashlib
import logging
from typing import AsyncIterator, List, Optional, Tuple

from ..adapters.base import BaseProvider, LLMMessage
from ..adapters.factory import build_provider
from ..core.config import Settings, get_settings
from ..core.exceptions import LLMError
from ..models.config import CSConfig
from ..utils.obfuscation import deobfuscate
from sqlalchemy import select
from . import runtime

log = logging.getLogger(__name__)


async def _load_runtime_config() -> dict:
    """Read the active CSConfig row (if any) to pick provider / model / protocol."""
    from ..core.database import session_scope

    try:
        async with session_scope() as session:
            row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
            if row is None:
                return {}
            return row.data or {}
    except Exception as e:
        log.warning("Could not load CSConfig: %s", e)
        return {}


def _resolved(settings: Settings, runtime_cfg: dict) -> dict:
    """Merge CSConfig over .env into the fields a provider is built from."""
    api_key = deobfuscate(runtime_cfg.get("model_api_key") or "") or settings.llm.api_key
    try:
        temperature = float(runtime_cfg.get("temperature", settings.llm.temperature))
    except (TypeError, ValueError):
        temperature = settings.llm.temperature
    try:
        max_tokens = int(runtime_cfg.get("max_tokens", settings.llm.max_tokens))
    except (TypeError, ValueError):
        max_tokens = settings.llm.max_tokens
    return {
        "api_key": api_key,
        "base_url": runtime_cfg.get("model_base_url") or settings.llm.base_url,
        "model": runtime_cfg.get("model_name") or settings.llm.model,
        "protocol": (runtime_cfg.get("protocol") or settings.llm.protocol or "openai").lower(),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def _fingerprint(r: dict) -> str:
    """Identity of a provider config. The key is hashed, never stored raw."""
    key_hash = hashlib.sha256((r["api_key"] or "").encode("utf-8")).hexdigest()[:16]
    return "|".join(
        str(x) for x in (
            r["protocol"], r["base_url"], r["model"],
            r["temperature"], r["max_tokens"], key_hash,
        )
    )


def _make(settings: Settings, r: dict) -> BaseProvider:
    return build_provider(
        api_key=r["api_key"],
        base_url=r["base_url"],
        model=r["model"],
        temperature=r["temperature"],
        max_tokens=r["max_tokens"],
        protocol=r["protocol"],
        settings=settings,
    )


async def _resolve(
    settings: Optional[Settings] = None, override: Optional[dict] = None
) -> Tuple[BaseProvider, bool]:
    """Return ``(provider, owned)``.

    ``owned=False`` → the shared singleton; the caller must NOT close it.
    ``owned=True`` → an ephemeral provider built from an unsaved ``override``
    (the console's "test connection" flow); the caller closes it. Overrides
    bypass the cache on purpose: caching one would evict the live provider
    over a config that was never saved.
    """
    settings = settings or get_settings()
    if override is not None:
        return _make(settings, _resolved(settings, override or {})), True

    resolved = _resolved(settings, await _load_runtime_config())
    fp = _fingerprint(resolved)

    async def _build() -> BaseProvider:
        return _make(settings, resolved)

    provider = await runtime.get_shared("llm_provider", fp, _build)
    return provider, False


class LLMService:
    """Façade over the shared provider; config edits still take effect at once."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings

    async def chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        override: Optional[dict] = None,
    ) -> str:
        provider, owned = await _resolve(self._settings, override)
        try:
            return await provider.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as e:
            log.exception("LLM chat failed: %s", e)
            raise LLMError(f"LLM 调用失败: {e}", details={"provider": provider.protocol}) from e
        finally:
            if owned and hasattr(provider, "aclose"):
                await provider.aclose()  # type: ignore[attr-defined]

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        override: Optional[dict] = None,
    ) -> AsyncIterator[str]:
        provider, owned = await _resolve(self._settings, override)
        try:
            async for piece in provider.stream_chat(
                messages, temperature=temperature, max_tokens=max_tokens
            ):
                yield piece
        except Exception as e:
            log.exception("LLM stream failed: %s", e)
            raise LLMError(f"LLM 流式调用失败: {e}", details={"provider": provider.protocol}) from e
        finally:
            if owned and hasattr(provider, "aclose"):
                await provider.aclose()  # type: ignore[attr-defined]

    async def resolved_temperature(self) -> float:
        """The effective temperature: CSConfig (console) over .env.

        Same precedence _resolve() applies when building the provider.
        RAGService needs the value up front — it caps the temperature for
        document-grounded answers and passes ``temperature=`` explicitly,
        which would otherwise silently override whatever the admin set in
        the console with the .env value.
        """
        settings = self._settings or get_settings()
        return float(_resolved(settings, await _load_runtime_config())["temperature"])

    async def test(self) -> tuple[bool, str]:
        provider, owned = await _resolve(self._settings)
        try:
            return await provider.test()
        finally:
            if owned and hasattr(provider, "aclose"):
                await provider.aclose()  # type: ignore[attr-defined]
