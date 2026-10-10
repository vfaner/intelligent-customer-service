"""RAG service: retrieval → prompt → stream generation → sources.

This is the heart of the customer-service pipeline. It must:

  * Embed the user's question.
  * Query the vector store with top_k + threshold from the live config.
  * Build a system prompt that keeps the LLM inside the retrieved chunks and
    refuses otherwise — unless ``rag.allow_model_knowledge`` is on, in which
    case it may answer from its own knowledge when retrieval comes up empty.
  * Stream tokens from the LLM while holding the matched sources so they
    can be attached to the final response.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..adapters.base import LLMMessage
from ..core.config import Settings, get_settings
from ..core.exceptions import LLMError, VectorStoreError
from ..core.i18n import AGENT_NAME, normalize_lang, pick
from ..prompts import (
    FALLBACK_SYSTEM_PROMPT,
    NO_HIT_REPLY,
    PARTIAL_KNOWLEDGE_RULE,
    RAG_SYSTEM_PROMPT,
    build_rag_user_prompt,
    output_lang_rule,
)
from ..vector_store.base import BaseVectorStore, VectorHit
from .embedding_service import QUERY_RATE_LIMIT_BUDGET, EmbeddingService
from .llm_service import LLMService

log = logging.getLogger(__name__)


def _format_hits(hits: List[VectorHit]) -> Tuple[List[str], List[Dict]]:
    """Render hits as numbered context blocks + a serializable sources list."""
    context: List[str] = []
    sources: List[Dict] = []
    for i, h in enumerate(hits, start=1):
        snippet = (h.text or "").strip()
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"
        context.append(f"[{i}] {snippet}")
        src = {
            "index": i,
            "chunk_id": h.chunk_id,
            "text": h.text,
            "score": round(float(h.score), 4),
        }
        md = h.metadata or {}
        for k in ("document_id", "filename", "source", "title", "heading", "chunk_index"):
            if k in md and md[k]:
                src[k] = md[k]
        sources.append(src)
    return context, sources


class RAGService:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        vector_store: Optional[BaseVectorStore] = None,
        embedder: Optional[EmbeddingService] = None,
        llm: Optional[LLMService] = None,
    ) -> None:
        self._settings = settings
        self._vector_store = vector_store
        # An injected store belongs to the caller (tests); the lazily built one
        # is the shared singleton from services.runtime and must never be
        # closed per request.
        self._injected_store = vector_store is not None
        self._embedder = embedder or EmbeddingService(settings)
        self._llm = llm or LLMService(settings)

    # --- accessors ----------------------------------------------------
    def settings(self) -> Settings:
        return self._settings or get_settings()

    async def vector_store(self) -> BaseVectorStore:
        if self._vector_store is None:
            from ..vector_store.factory import get_shared_vector_store
            self._vector_store = await get_shared_vector_store(self._settings)
        return self._vector_store

    # --- runtime config ------------------------------------------------
    async def _rag_config(self) -> Dict[str, Any]:
        """Resolve RAG options: CSConfig (set in the admin UI) over .env.

        Read per request so toggling a switch in the console takes effect
        immediately — matching how LLMService and EmbeddingService behave.
        """
        s = self.settings()
        cfg: Dict[str, Any] = {
            "top_k": s.rag.top_k,
            "similarity_threshold": s.rag.similarity_threshold,
            "max_context_chars": s.rag.max_context_chars,
            "allow_model_knowledge": s.rag.allow_model_knowledge,
            "relevance_threshold": s.rag.relevance_threshold,
        }
        try:
            from sqlalchemy import select

            from ..core.database import session_scope
            from ..models.config import CSConfig

            async with session_scope() as session:
                row = (await session.execute(select(CSConfig).limit(1))).scalar_one_or_none()
                rag = (row.data or {}).get("rag") if row else None
            if rag:
                if rag.get("top_k"):
                    cfg["top_k"] = int(rag["top_k"])
                if rag.get("similarity_threshold") is not None:
                    cfg["similarity_threshold"] = float(rag["similarity_threshold"])
                if rag.get("allow_model_knowledge") is not None:
                    cfg["allow_model_knowledge"] = bool(rag["allow_model_knowledge"])
                if rag.get("relevance_threshold") is not None:
                    cfg["relevance_threshold"] = float(rag["relevance_threshold"])
        except Exception as e:
            log.debug("could not read rag config from DB: %s", e)
        return cfg

    # --- pipeline -----------------------------------------------------
    async def _retrieve(self, question: str, cfg: Dict[str, Any]) -> List[VectorHit]:
        # Interactive path: a 429-hammered embedding vendor must surface an
        # error in seconds, not ride out the multi-minute ingestion budget.
        vecs = await self._embedder.embed([question], rate_limit_budget=QUERY_RATE_LIMIT_BUDGET)
        if not vecs:
            return []
        store = await self.vector_store()
        try:
            hits = await store.query(
                vecs[0],
                top_k=max(1, int(cfg["top_k"])),
                threshold=float(cfg["similarity_threshold"]),
            )
        except VectorStoreError:
            raise
        except Exception as e:
            raise VectorStoreError(f"vector query failed: {e}") from e
        return hits

    async def _build_messages(
        self,
        question: str,
        history: Optional[List[LLMMessage]] = None,
        agent_name: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> Tuple[List[LLMMessage], List[Dict], bool]:
        """Build the prompt. Returns (messages, sources, used_model_knowledge)."""
        lang = normalize_lang(lang)
        agent_name = agent_name or pick(AGENT_NAME, lang)
        cfg = await self._rag_config()
        hits = await self._retrieve(question, cfg)
        allow = bool(cfg["allow_model_knowledge"])

        # With CJK embeddings even unrelated text scores ~0.65-0.71, so "we got
        # hits" is not the same as "we got relevant hits". `relevance_threshold`
        # is the measured dividing line (on-topic ≥0.81 here); below it the
        # documents almost certainly don't answer the question, so let the
        # toggle decide what to do.
        best = hits[0].score if hits else 0.0
        looks_relevant = best >= float(cfg["relevance_threshold"])

        if allow and not looks_relevant:
            # Nothing convincingly relevant but fallback is permitted: drop the
            # "only use the documents" framing entirely, otherwise the model
            # still refuses even though it was told it may answer freely.
            log.info(
                "RAG fallback to model knowledge (best score %.4f < relevance %.2f)",
                best, float(cfg["relevance_threshold"]),
            )
            system = FALLBACK_SYSTEM_PROMPT.format(agent_name=agent_name)
            system += output_lang_rule(lang)
            messages: List[LLMMessage] = [LLMMessage(role="system", content=system)]
            if history:
                messages.extend(history)
            messages.append(LLMMessage(role="user", content=question))
            return messages, [], True

        context_blocks, sources = _format_hits(hits)
        system = RAG_SYSTEM_PROMPT.format(
            agent_name=agent_name,
            refusal_reply=pick(NO_HIT_REPLY, lang),
        )
        if allow:
            # Retrieval looks relevant, but the documents may only cover part of
            # the question — let the model fill the gaps.
            system += PARTIAL_KNOWLEDGE_RULE
        # Language rule goes last so it isn't buried under the other blocks.
        system += output_lang_rule(lang)
        user_prompt = build_rag_user_prompt(
            question, context_blocks, max_chars=int(cfg["max_context_chars"]), lang=lang
        )
        messages = [LLMMessage(role="system", content=system)]
        if history:
            messages.extend(history)
        messages.append(LLMMessage(role="user", content=user_prompt))
        return messages, sources, False

    async def ask(
        self,
        question: str,
        *,
        history: Optional[List[LLMMessage]] = None,
        agent_name: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> Tuple[str, List[Dict]]:
        lang = normalize_lang(lang)
        messages, sources, from_model = await self._build_messages(
            question, history, agent_name, lang
        )
        s = self.settings()
        if not sources and not from_model:
            # Nothing retrieved and fallback is off — refuse without paying for
            # a round-trip that would only produce the canned reply anyway.
            # Never sees the LLM, so this must be a real translation.
            return pick(NO_HIT_REPLY, lang), []
        # A model-knowledge answer isn't constrained by documents, so the low
        # anti-hallucination temperature doesn't apply.
        temp = s.llm.temperature if from_model else min(s.llm.temperature, 0.3)
        answer = await self._llm.chat(messages, temperature=temp)
        return answer, sources

    async def stream_ask(
        self,
        question: str,
        *,
        history: Optional[List[LLMMessage]] = None,
        agent_name: Optional[str] = None,
        lang: Optional[str] = None,
    ) -> AsyncIterator[Tuple[str, List[Dict] | None]]:
        """Yields ``(token, sources_or_None)``.

        Sources are emitted exactly once, on the final event of the stream.
        The caller should treat any event with ``sources`` as the terminator.
        If retrieval finds nothing and ``rag.allow_model_knowledge`` is off, a
        single refusal event is emitted; when it's on, the model answers from
        its own knowledge and the sources list comes back empty.

        ``lang`` pins the answer's language (see ``OUTPUT_LANG_RULE``) and picks
        the translation of the canned refusal.
        """
        lang = normalize_lang(lang)
        messages, sources, from_model = await self._build_messages(
            question, history, agent_name, lang
        )
        if not sources and not from_model:
            yield pick(NO_HIT_REPLY, lang), []
            return
        s = self.settings()
        temp = s.llm.temperature if from_model else min(s.llm.temperature, 0.3)
        yielded_any = False
        async for token in self._llm.stream_chat(messages, temperature=temp):
            yielded_any = True
            yield token, None
        if not yielded_any:
            # Empty stream — fall back to the canned refusal.
            yield pick(NO_HIT_REPLY, lang), []
        yield "", sources  # terminator carrying sources

    async def aclose(self) -> None:
        # EmbeddingService.aclose is a reference drop (the embedder is shared).
        await self._embedder.aclose()
        # The shared vector store is closed by runtime.close_shared() at app
        # shutdown — closing it per request would break connection reuse and
        # could cut a concurrent retrieval. Only caller-injected stores (tests)
        # are closed here, preserving the old ownership semantics.
        if self._vector_store is not None and self._injected_store:
            await self._vector_store.aclose()
