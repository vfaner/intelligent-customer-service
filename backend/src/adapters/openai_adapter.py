"""OpenAI-compatible chat adapter (works for OpenAI + 12+ gateways)."""
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


class OpenAIStyleProvider(BaseProvider):
    protocol = "openai"

    def __init__(self, config: LLMConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config)
        if not config.base_url:
            raise LLMProtocolError("OpenAI provider: base_url is empty")
        if not config.model:
            raise LLMProtocolError("OpenAI provider: model is empty")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout)

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.config.api_key:
            h["Authorization"] = f"Bearer {self.config.api_key}"
        return h

    def _payload(
        self,
        messages: List[LLMMessage],
        temperature: float,
        max_tokens: int,
        stream: bool,
    ) -> dict:
        return {
            "model": self.config.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": stream,
        }

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
        try:
            return data["choices"][0]["message"]["content"] or ""
        except Exception as e:
            raise LLMProtocolError(f"Unexpected response: {json.dumps(data, ensure_ascii=False)[:500]}") from e

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
        # adapters.base — once tokens have reached the client a restart would
        # duplicate text, and mid-stream vendor errors aren't LLMRateLimited.
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
                if data == "[DONE]":
                    break
                data_lines += 1
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                parsed += 1
                try:
                    delta = obj["choices"][0].get("delta") or {}
                    piece = delta.get("content")
                except Exception:
                    piece = None
                if piece:
                    yield piece
        if data_lines and not parsed:
            # A gateway that answered 200 with HTML/plain text (wrong base
            # URL, captive portal, proxy error page) used to produce a silent
            # empty stream, which the RAG layer masked as "no answer found".
            raise LLMProtocolError(
                f"模型流式响应中没有可解析的数据行（收到 {data_lines} 行）"
                f"——请检查 Base URL 与协议（openai/anthropic）是否匹配"
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
