"""OpenAI-compatible embedding client.

Works with any vendor exposing ``POST {base}/embeddings`` in the OpenAI
schema: OpenAI, DeepSeek (if enabled), Bailian/Qwen, Zhipu, Volcengine,
Moonshot, etc. Batches large requests and tolerates transient failures.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import List

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..core.exceptions import make_retry_predicate
from .base import BaseEmbedder, EmbeddingError

log = logging.getLogger(__name__)


class _BatchTooLarge(Exception):
    """The vendor rejected the request purely because the batch was too big.

    Deliberately NOT an ``EmbeddingError`` — tenacity retries those, and
    re-sending an identical oversized batch can never succeed. ``embed()``
    catches this, shrinks the batch, and retries the same slice.
    """

    def __init__(self, message: str, allowed: int | None = None) -> None:
        super().__init__(message)
        self.allowed = allowed


class _RateLimited(Exception):
    """Vendor said "too many requests" (HTTP 429).

    Also not an ``EmbeddingError``: the fix is to slow down and wait, which
    ``embed()`` does with an escalating inter-request delay. Bubbling it up as
    a plain error would abort a 700-chunk ingest a few seconds in.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OpenAICompatibleEmbedder(BaseEmbedder):
    """Async OpenAI-compatible embedding client."""

    # Measured on Volcengine ARK with ~700-char chunks (2026-08):
    #
    #   * A burst of 20 back-to-back requests × 8 texts: 13 succeed, 7 get 429,
    #     total 4.4s → ~24 texts/sec end to end. The 429s are cheap; retrying
    #     immediately is what keeps throughput up.
    #   * Adding a fixed delay between requests made things dramatically WORSE
    #     (1.0s gap → 11/12 requests 429'd). The limiter is bursty/token-bucket
    #     shaped, so pausing doesn't buy goodwill — it just wastes wall-clock.
    #   * Concurrency also hurt badly (100 chunks: 3.4s serial vs 211s at 4).
    #
    # Hence: serial, no inter-request pacing, and DON'T shrink the batch on a
    # 429 (that error is about request *rate*, not batch size — smaller batches
    # mean more requests, which makes it worse).
    #
    # Retrying is budgeted by TIME rather than attempt count: ARK's quota can
    # take 90s+ to refill, so an attempt-capped retry gave up too early and
    # every ingest died after a single slice. See RATE_LIMIT_BUDGET below.
    DEFAULT_BATCH = 8
    DEFAULT_TIMEOUT = 60.0
    DEFAULT_CONCURRENCY = 1
    MIN_CONCURRENCY = 1
    MIN_BATCH = 1
    # 429 handling, budgeted by TIME not by attempt count.
    #
    # ARK's embedding quota refills on the order of 1-2 minutes, so a
    # count-based cap (40 tries × ~0.35s ≈ 44s of waiting) always gave up too
    # early — every ingest died after a single slice and the user had to click
    # again. Ingestion is a background batch job: waiting a few minutes is far
    # better than failing and needing manual restarts.
    RATE_LIMIT_BUDGET = 600.0   # seconds to keep trying one batch before failing
    RATE_RETRY_MIN = 1.0        # first pause after a 429
    RATE_RETRY_MAX = 20.0       # ceiling for a single pause

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dim: int = 0,
        *,
        batch_size: int = DEFAULT_BATCH,
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = DEFAULT_CONCURRENCY,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise EmbeddingError("Embedding base_url is empty")
        if not model:
            raise EmbeddingError("Embedding model is empty")
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.concurrency = max(self.MIN_CONCURRENCY, concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/embeddings"):
            return base
        return f"{base}/embeddings"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # Vendors cap how many strings one /embeddings call may carry, and they
    # don't advertise the limit — Volcengine ARK allows 10, OpenAI allows
    # 2048. Rather than hard-coding the smallest value (and slowing everyone
    # down), start optimistic and shrink when a vendor complains.
    _BATCH_LIMIT_HINTS = (
        "input limit exceeded",
        "too many inputs",
        "batch size",
        "exceeds the maximum",
        "at most",
    )

    @classmethod
    def _is_batch_too_large(cls, message: str) -> bool:
        low = message.lower()
        return any(h in low for h in cls._BATCH_LIMIT_HINTS)

    @staticmethod
    def _parse_max_from_error(message: str) -> int | None:
        """Pull the allowed maximum out of the error text when it's stated.

        ARK says: "input limit exceeded: max 10, got 13" — honouring that
        number means we resize once instead of halving repeatedly.
        """
        for pat in (r"max(?:imum)?[^\d]{0,12}(\d+)", r"at most[^\d]{0,12}(\d+)"):
            m = re.search(pat, message, re.IGNORECASE)
            if m:
                try:
                    n = int(m.group(1))
                    if 0 < n <= 4096:
                        return n
                except ValueError:
                    pass
        return None

    @retry(
        # 4xx (except 429) carries upstream_status and is NOT retried — a bad
        # key or model name fails identically every time. 429 never reaches
        # this predicate (raised as _RateLimited, handled by embed()'s loop).
        retry=retry_if_exception(make_retry_predicate(httpx.HTTPError, EmbeddingError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    async def _call_once(self, batch: List[str]) -> List[List[float]]:
        payload = {"model": self.model, "input": batch}
        try:
            resp = await self._client.post(self._endpoint(), headers=self._headers(), json=payload)
        except httpx.HTTPError as e:
            raise EmbeddingError(f"HTTP error calling embeddings: {e}") from e

        if resp.status_code >= 400:
            body = resp.text[:400]
            if resp.status_code == 429:
                ra = resp.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra else None
                except ValueError:
                    wait = None
                raise _RateLimited(body, wait)
            if resp.status_code == 400 and self._is_batch_too_large(body):
                # Signal the batch-size problem distinctly so `embed()` can
                # retry smaller instead of tenacity blindly repeating a call
                # that will always fail.
                raise _BatchTooLarge(body, self._parse_max_from_error(body))
            raise EmbeddingError(
                f"Embedding API returned HTTP {resp.status_code}: {body[:300]}",
                upstream_status=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as e:
            raise EmbeddingError(f"Embedding response is not JSON: {e}") from e

        items = data.get("data") or []
        if not isinstance(items, list) or not items:
            raise EmbeddingError(f"Embedding response missing 'data': {data!r}")
        items.sort(key=lambda x: x.get("index", 0))
        vectors: List[List[float]] = []
        for item in items:
            vec = item.get("embedding")
            if not isinstance(vec, list):
                raise EmbeddingError(f"Embedding item missing 'embedding': {item!r}")
            vectors.append([float(x) for x in vec])
        if len(vectors) != len(batch):
            raise EmbeddingError(
                f"Embedding count mismatch: got {len(vectors)}, expected {len(batch)}"
            )
        return vectors

    async def embed(
        self, texts: List[str], *, rate_limit_budget: float | None = None
    ) -> List[List[float]]:
        cleaned = [(t or "").strip() for t in texts]
        if not cleaned:
            return []
        budget = self.RATE_LIMIT_BUDGET if rate_limit_budget is None else float(rate_limit_budget)

        # Probe with one batch first: it settles the vendor's batch limit (and
        # surfaces auth/model errors) before we fan out and multiply the
        # failure. After that, run batches concurrently.
        out: List[List[float]] = []
        head = cleaned[: self.batch_size]
        out.extend(await self._embed_slice_serial(head, budget))
        rest = cleaned[len(head):]
        if not rest:
            if self.dim <= 0 and out:
                self.dim = len(out[0])
            return out

        batches = [rest[i : i + self.batch_size] for i in range(0, len(rest), self.batch_size)]
        sem = asyncio.Semaphore(max(self.MIN_CONCURRENCY, self.concurrency))

        async def run(batch: List[str]) -> List[List[float]]:
            async with sem:
                return await self._embed_slice_serial(batch, budget)

        # gather preserves order, so vectors still line up with `texts`.
        results = await asyncio.gather(*(run(b) for b in batches))
        for r in results:
            out.extend(r)

        if self.dim <= 0 and out:
            self.dim = len(out[0])
        return out

    async def _embed_slice_serial(
        self, batch: List[str], budget: float | None = None
    ) -> List[List[float]]:
        """Embed one batch, handling batch-too-large and rate limiting.

        On 429 this both waits and raises the global pace, so sibling requests
        slow down too rather than each discovering the limit independently.

        ``budget`` caps the total 429 wait for this call; ``None`` falls back
        to the class default (the long ingestion budget).
        """
        if budget is None:
            budget = self.RATE_LIMIT_BUDGET
        rate_hits = 0
        rl_started: float | None = None      # when this batch first hit a 429
        while True:
            # A batch may need re-slicing if the vendor shrank our size while
            # this call was queued behind the semaphore.
            if len(batch) > self.batch_size:
                out: List[List[float]] = []
                for i in range(0, len(batch), self.batch_size):
                    out.extend(
                        await self._embed_slice_serial(batch[i : i + self.batch_size], budget)
                    )
                return out
            try:
                return await self._call_once(batch)
            except _BatchTooLarge as e:
                new_size = e.allowed or max(self.MIN_BATCH, self.batch_size // 2)
                new_size = max(self.MIN_BATCH, min(new_size, self.batch_size - 1))
                if new_size >= self.batch_size:
                    raise EmbeddingError(
                        f"Embedding API rejected a batch of {len(batch)} and no smaller "
                        f"size could be derived: {e}"
                    ) from e
                log.info(
                    "Embedding batch of %d rejected by vendor; shrinking batch size %d → %d",
                    len(batch), self.batch_size, new_size,
                )
                self.batch_size = new_size
                continue
            except _RateLimited as e:
                rate_hits += 1
                waited = time.monotonic() - rl_started if rl_started else 0.0
                if rl_started is None:
                    rl_started = time.monotonic()
                    waited = 0.0

                if waited >= budget:
                    raise EmbeddingError(
                        f"向量模型接口持续返回 429，已等待 {waited / 60:.1f} 分钟仍未恢复"
                        f"（重试 {rate_hits} 次）。可能是账号配额已用尽。"
                        f"请稍后重试，或在「模型配置 → 向量模型」换用配额更宽松的服务。"
                        f"原始错误：{e}"
                    ) from e

                # Escalate the pause: quick retries handle a momentary burst,
                # longer ones ride out an exhausted quota bucket.
                wait = e.retry_after or min(
                    self.RATE_RETRY_MAX, self.RATE_RETRY_MIN * (2 ** min(rate_hits - 1, 5))
                )
                # Log sparsely — this can loop for minutes.
                if rate_hits <= 2 or rate_hits % 15 == 0:
                    log.info(
                        "Embedding rate-limited (%d 次, 已等 %.0fs/%.0fs 预算), "
                        "%.0fs 后重试 batch=%d",
                        rate_hits, waited, budget, wait, len(batch),
                    )
                await asyncio.sleep(wait)
                continue

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenAICompatibleEmbedder":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
