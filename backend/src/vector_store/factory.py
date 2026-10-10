"""Vector store factory: pick Chroma, Qdrant or Milvus from settings."""
from __future__ import annotations

import asyncio
from typing import Optional

from ..core.config import Settings, get_settings
from ..core.exceptions import VectorStoreError
from ..services import runtime
from .base import BaseVectorStore
from .chroma_store import ChromaStore
from .milvus_store import MilvusStore
from .qdrant_store import QdrantStore


def build_vector_store(
    settings: Optional[Settings] = None,
    *,
    embedding_dimension_override: Optional[int] = None,
) -> BaseVectorStore:
    settings = settings or get_settings()
    v = settings.vector_db
    # Used by the reindex pipeline to recreate the collection at the newly
    # probed dimension before the .env/UI value has caught up. Chroma ignores
    # this (its dimension is inferred from the first inserted vector).
    dim = int(embedding_dimension_override) if embedding_dimension_override else v.embedding_dim
    if v.provider == "chroma":
        return ChromaStore(
            persist_dir=v.chroma_persist_dir,
            collection=v.collection,
            embedding_dim=dim,
        )
    if v.provider == "qdrant":
        return QdrantStore(
            host=v.host,
            port=v.port,
            collection=v.collection,
            embedding_dim=dim,
        )
    if v.provider == "milvus":
        return MilvusStore(
            collection=v.collection,
            embedding_dim=dim,
            uri=v.milvus_uri,
            host=v.host,
            port=v.milvus_port,
            token=v.milvus_token,
            db_name=v.milvus_db_name,
        )
    raise VectorStoreError(f"Unsupported vector DB provider: {v.provider}")


def _store_fingerprint(v, dim: int) -> str:
    """Every setting the store was built from, so any change forces a rebuild."""
    return "|".join(
        str(x)
        for x in (
            v.provider, v.host, v.port, v.collection, v.chroma_persist_dir,
            v.milvus_uri, v.milvus_port, v.milvus_token, v.milvus_db_name, dim,
        )
    )


async def get_shared_vector_store(
    settings: Optional[Settings] = None,
    *,
    embedding_dimension_override: Optional[int] = None,
) -> BaseVectorStore:
    """Cached ``build_vector_store``: same config → same client, connections reused.

    The store constructors do blocking I/O (Chroma opens its persistent DB,
    Qdrant checks/creates the collection over HTTP, Milvus connects) — work
    that used to run on the event loop once per request. Now it happens once
    per distinct config, inside a worker thread; see ``services.runtime`` for
    the caching / retirement semantics.
    """
    settings = settings or get_settings()
    v = settings.vector_db
    dim = int(embedding_dimension_override) if embedding_dimension_override else v.embedding_dim
    fp = _store_fingerprint(v, dim)

    async def _build() -> BaseVectorStore:
        return await asyncio.to_thread(
            build_vector_store,
            settings,
            embedding_dimension_override=embedding_dimension_override,
        )

    return await runtime.get_shared("vector_store", fp, _build)
