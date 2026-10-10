"""System prompts used by the LLM services."""
from .system_prompts import (
    FALLBACK_SYSTEM_PROMPT,
    NO_HIT_REPLY,
    OUTPUT_LANG_RULE,
    PARTIAL_KNOWLEDGE_RULE,
    RAG_NO_HIT_REPLY,
    RAG_SYSTEM_PROMPT,
    build_rag_user_prompt,
    output_lang_rule,
)

__all__ = [
    "RAG_SYSTEM_PROMPT",
    "RAG_NO_HIT_REPLY",
    "NO_HIT_REPLY",
    "OUTPUT_LANG_RULE",
    "output_lang_rule",
    "FALLBACK_SYSTEM_PROMPT",
    "PARTIAL_KNOWLEDGE_RULE",
    "build_rag_user_prompt",
]
