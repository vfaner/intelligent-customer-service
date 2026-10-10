"""Base LLM provider with chat + stream + test.

Subclasses implement two protocols:
  - OpenAI:    POST {base}/chat/completions
  - Anthropic: POST {base}/messages

Both must support streaming via SSE.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional, Tuple


class LLMProtocolError(Exception):
    """Raised when the provider returns an unexpected payload."""

    def __init__(self, message: str, *, upstream_status: Optional[int] = None) -> None:
        super().__init__(message)
        #: HTTP status the vendor returned, when this wraps an HTTP response.
        #: Drives ``core.exceptions.is_fatal_http_status`` (4xx = no retry).
        self.upstream_status = upstream_status


class LLMRateLimited(Exception):
    """Provider returned HTTP 429.

    Separate from ``LLMProtocolError`` because the response is to wait, not to
    treat the request as malformed. Vendors like Volcengine ARK exhaust an
    account-level quota and need tens of seconds to recover — a couple of fast
    retries isn't enough.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# Retry budget for rate-limited chat calls. Measured against ARK: the quota
# refills on the order of a minute. Chat is interactive, so the ceiling is much
# lower than ingestion's — better to surface an error than leave the user
# staring at a spinner for minutes.
RATE_LIMIT_BUDGET = 45.0     # seconds to keep retrying before giving up
RATE_RETRY_MIN = 1.0
RATE_RETRY_MAX = 8.0


def rate_limit_wait(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff for a rate-limited attempt (1-indexed)."""
    if retry_after:
        return min(retry_after, RATE_RETRY_MAX)
    return min(RATE_RETRY_MAX, RATE_RETRY_MIN * (2 ** min(attempt - 1, 4)))


def parse_retry_after(headers) -> float | None:
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class LLMMessage:
    role: str   # system | user | assistant
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 60.0
    extra: dict = field(default_factory=dict)


class BaseProvider(ABC):
    """Common shape for OpenAI / Anthropic protocol adapters."""

    protocol: str = "base"   # "openai" | "anthropic"

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @abstractmethod
    async def chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """One-shot chat completion; return the assistant content."""

    @abstractmethod
    async def stream_chat(
        self,
        messages: List[LLMMessage],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Yield assistant content incrementally. Empty string on end."""

    async def test(self) -> Tuple[bool, str]:
        """Smoke test. Returns (ok, message)."""
        msgs = [
            LLMMessage(role="system", content="You are a helpful assistant."),
            LLMMessage(role="user", content="Please reply with the single word: OK"),
        ]
        try:
            reply = (await self.chat(msgs, temperature=0.0, max_tokens=20)).strip()
            return True, f"测试成功 - 模型回复: {reply[:120]}"
        except Exception as e:
            return False, f"测试失败: {e}"
