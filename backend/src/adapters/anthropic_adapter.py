"""Anthropic Messages API adapter.

Implements ``POST {base}/messages`` with SSE streaming. System prompt is a
top-level field rather than a message.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, List, Optional

import httpx
from tenacity import retry, retry_if_exception

from ..core.exceptions import make_retry_predicate
from .base import (
    RETRY_STOP,
    RETRY_WAIT,
    BaseProvider,
    LLMConfig,
    LLMMessage,
    LLMProtocolError,
    LLMRateLimited,
    call_with_rate_limit_retry,
    parse_retry_after,
    stream_with_rate_limit_retry,
)


def _split_system(messages: List[LLMMessage]) -> tuple[str, List[dict]]:
    """Anthropic uses a top-level ``system`` field, separate from messages."""
    system_parts: List[str] = []
    rest: List[dict] = []
    for m in messages:
        if m.role == "system":
            system_parts.append(m.content)
        else:
            rest.append({"role": m.role, "content": m.content})
    return "\n\n".join(system_parts), rest


class AnthropicStyleProvider(BaseProvider):
    protocol = "anthropic"

    DEFAULT_VERSION = "2023-06-01"

    def __init__(self, config: LLMConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config)
        if not config.base_url:
            raise LLMProtocolError("Anthropic provider: base_url is empty")
        if not config.model:
            raise LLMProtocolError("Anthropic provider: model is empty")
        self.version = str(config.extra.get("anthropic_version") or self.DEFAULT_VERSION)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout)

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/messages"):
            return base
        return f"{base}/messages"

    def _headers(self) -> dict:
        """Auth headers for the Messages API.

        Anthropic's own API authenticates with ``x-api-key``, but
        Anthropic-*compatible* gateways often expect the OpenAI-style
        ``Authorization: Bearer`` instead — Volcengine ARK returns 401 for
        ``x-api-key`` alone. Sending both satisfies either side; each ignores
        the header it doesn't use.
        """
        key = self.config.api_key or ""
        return {
            "Content-Type": "application/json",
            "x-api-key": key,
            "Authorization": f"Bearer {key}",
            "anthropic-version": self.version,
        }

    def _payload(
        self,
        messages: List[LLMMessage],
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict:
        system_text, rest = _split_system(messages)
        payload: dict = {
            "model": self.config.model,
            "messages": rest,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        if system_text:
            payload["system"] = system_text
        if stream:
            payload["stream"] = True
        return payload

    async def chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        payload = self._payload(
            messages,
            temperature if temperature is not None else self.config.temperature,
            max_tokens if max_tokens is not None else self.config.max_tokens,
            stream=False,
        )
        return await call_with_rate_limit_retry(lambda: self._chat_once(payload))

    @retry(
        # 4xx (except 429) carries upstream_status and is NOT retried — a bad
        # key or model name fails identically every time.
        retry=retry_if_exception(make_retry_predicate(httpx.HTTPError, LLMProtocolError)),
        stop=RETRY_STOP,
        wait=RETRY_WAIT,
        reraise=True,
    )
    async def _chat_once(self, payload: dict) -> str:
        try:
            resp = await self._client.post(self._endpoint(), headers=self._headers(), json=payload)
        except httpx.HTTPError as e:
            raise LLMProtocolError(f"HTTP error: {e}") from e
        if resp.status_code == 429:
            raise LLMRateLimited(resp.text[:300], parse_retry_after(resp.headers))
        if resp.status_code >= 400:
            raise LLMProtocolError(
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                upstream_status=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as e:
            raise LLMProtocolError(f"Response is not JSON: {e}") from e
        content = data.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text") or ""
        raise LLMProtocolError(f"Unexpected response: {json.dumps(data, ensure_ascii=False)[:500]}")

    async def stream_chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        payload = self._payload(
            messages,
            temperature if temperature is not None else self.config.temperature,
            max_tokens if max_tokens is not None else self.config.max_tokens,
            stream=True,
        )
        headers = self._headers()
        headers["Accept"] = "text/event-stream"

        # 429s before the stream starts are retried by the shared loop in
        # adapters.base — once tokens have been yielded a restart would
        # duplicate output, and mid-stream vendor errors aren't LLMRateLimited.
        async for piece in stream_with_rate_limit_retry(
            lambda: self._stream_once(headers, payload)
        ):
            yield piece

    async def _stream_once(self, headers: dict, payload: dict) -> AsyncIterator[str]:
        data_lines = 0
        parsed = 0
        async with self._client.stream(
            "POST", self._endpoint(), headers=headers, json=payload
        ) as resp:
            if resp.status_code == 429:
                body = await resp.aread()
                raise LLMRateLimited(
                    body[:300].decode("utf-8", "ignore"), parse_retry_after(resp.headers)
                )
            if resp.status_code >= 400:
                body = await resp.aread()
                raise LLMProtocolError(
                    f"HTTP {resp.status_code}: {body[:500].decode('utf-8', 'ignore')}",
                    upstream_status=resp.status_code,
                )
            async for raw in resp.aiter_lines():
                if not raw:
                    continue
                line = raw.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                data_lines += 1
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                parsed += 1
                etype = ev.get("type")
                if etype == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if text:
                            yield text
                elif etype == "message_stop":
                    break
                elif etype == "error":
                    # Mid-stream vendor error (overloaded, quota exhausted…).
                    # Without this the stream simply ends and the failure is
                    # indistinguishable from an empty answer.
                    err = ev.get("error") or {}
                    raise LLMProtocolError(
                        f"Anthropic 流式返回错误事件 {err.get('type', 'unknown')}: "
                        f"{str(err.get('message', ''))[:300]}"
                    )
        if data_lines and not parsed:
            raise LLMProtocolError(
                f"模型流式响应中没有可解析的数据行（收到 {data_lines} 行）"
                f"——请检查 Base URL 与协议（anthropic/openai）是否匹配"
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
