"""Qdrant-backed vector store.

Supports either an in-memory instance (``location=":memory:"``) or a
remote gRPC server. Uses cosine similarity; converts the raw score to
``[0, 1]`` for consistency with the Chroma adapter.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from .base import (
    BaseVectorStore,
    ChunkRecord,
    VectorHit,
    VectorStoreError,
    to_thread as _to_thread,
)

log = logging.getLogger(__name__)

# Namespace for turning "{doc_id}:{index}" chunk ids into stable UUIDs.
_QDRANT_ID_NAMESPACE = uuid.UUID("6f1e5c9a-3f7d-4c2b-9a41-7e5b0d2c8f13")


def _point_id(chunk_id: str) -> str:
    """Map an arbitrary chunk id string to a deterministic UUID.

    Qdrant only accepts unsigned integers or UUIDs as point ids, but our
    chunk ids look like ``{document_id}:{index}``. Hashing them into a UUID5
    keeps upserts idempotent (same chunk id → same point).
    """
    return str(uuid.uuid5(_QDRANT_ID_NAMESPACE, chunk_id))


class QdrantStore(BaseVectorStore):
    name = "qdrant"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6333,
        collection: str = "knowledge_base",
        embedding_dim: int = 0,
        location: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient  # type: ignore
            from qdrant_client.http import models as qm  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise VectorStoreError("qdrant-client is not installed. `pip install qdrant-client`") from e

        if location:
            self._client = QdrantClient(location=location, api_key=api_key)
        else:
            self._client = QdrantClient(host=host, port=port, api_key=api_key, prefer_grpc=False)
        self._qm = qm
        self.collection = collection
        self.embedding_dim = embedding_dim

        # Ensure collection exists
        if not self._client.collection_exists(collection_name=collection):
            # Qdrant collections have a fixed vector size. Clamping a bad dim
            # to 1 would silently create a collection no real embedding fits
            # (an existing collection is fine with dim=0 — it carries its own).
            if embedding_dim <= 0:
                raise VectorStoreError(
                    "Qdrant requires a fixed vector dimension when creating a collection. "
                    "Set VECTOR_DB_EMBEDDING_DIM (and EMBEDDING_DIM) to match your model."
                )
            vectors_config = qm.VectorParams(
                size=embedding_dim, distance=qm.Distance.COSINE
            )
            self._client.create_collection(collection_name=collection, vectors_config=vectors_config)
            log.info("Created Qdrant collection %s (dim=%s)", collection, embedding_dim)
        log.info("QdrantStore ready: %s:%s/%s", host, port, collection)

    @staticmethod
    def _score_from_cosine(raw: float) -> float:
        # Qdrant returns cosine similarity in [-1, 1]; map to [0, 1].
        return max(0.0, min(1.0, (float(raw) + 1.0) / 2.0))

    async def add(
        self,
        document_id: str,
        chunks: List[ChunkRecord],
        vectors: List[List[float]],
    ) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise VectorStoreError(
                f"chunks/vectors length mismatch: {len(chunks)} vs {len(vectors)}"
            )
        qm = self._qm
        points: List[qm.PointStruct] = []
        for c, v in zip(chunks, vectors):
            payload: Dict[str, Any] = {
                "text": c.text,
                "document_id": document_id,
                # Qdrant point ids must be UUID/int, so keep the human-readable
                # chunk id in the payload and surface it again on query.
                "chunk_id": c.chunk_id,
            }
            payload.update(c.metadata or {})
            points.append(
                qm.PointStruct(id=_point_id(c.chunk_id), vector=list(v), payload=payload)
            )

        def _do():
            self._client.upsert(collection_name=self.collection, points=points, wait=True)
            return len(points)

        return await _to_thread(_do)

    async def query(
        self,
        vector: List[float],
        top_k: int = 5,
        threshold: float = 0.0,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[VectorHit]:
        if not vector:
            return []
        qm = self._qm

        query_filter = None
        if where and "document_id" in where:
            query_filter = qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="document_id", match=qm.MatchValue(value=where["document_id"])
                    )
                ]
            )

        def _do():
            # qdrant-client >= 1.10 renamed `search()` to `query_points()` and
            # wraps the results in a QueryResponse. Support both.
            if hasattr(self._client, "query_points"):
                resp = self._client.query_points(
                    collection_name=self.collection,
                    query=list(vector),
                    limit=max(1, top_k),
                    with_payload=True,
                    query_filter=query_filter,
                )
                return getattr(resp, "points", resp)
            return self._client.search(
                collection_name=self.collection,
                query_vector=list(vector),
                limit=max(1, top_k),
                with_payload=True,
                query_filter=query_filter,
            )

        results = await _to_thread(_do)
        hits: List[VectorHit] = []
        for r in results:
            score = self._score_from_cosine(r.score)
            if score < threshold:
                continue
            payload = r.payload or {}
            hits.append(
                VectorHit(
                    # Prefer the original chunk id we stashed in the payload;
                    # fall back to the UUID point id for older records.
                    chunk_id=str(payload.get("chunk_id") or r.id),
                    text=payload.get("text", ""),
                    score=score,
                    metadata={
                        k: v for k, v in payload.items() if k not in ("text", "chunk_id")
                    },
                )
            )
        return hits

    async def delete_document(self, document_id: str) -> int:
        qm = self._qm

        def _count():
            res = self._client.count(
                collection_name=self.collection,
                count_filter=qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=document_id))]),
                exact=True,
            )
            return int(res.count)

        n = await _to_thread(_count)
        if n == 0:
            return 0

        def _do():
            self._client.delete(
                collection_name=self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=document_id))]
                    )
                ),
                wait=True,
            )
            return n

        return await _to_thread(_do)

    async def reset(self) -> None:
        """Drop and recreate the collection at the configured dimension."""
        # Check before dropping anything: recreating at a clamped dim=1 would
        # destroy a working collection and leave an unusable one behind.
        if self.embedding_dim <= 0:
            raise VectorStoreError(
                "Qdrant requires a fixed vector dimension when creating a collection. "
                "Set VECTOR_DB_EMBEDDING_DIM (and EMBEDDING_DIM) to match your model."
            )

        def _do():
            qm = self._qm
            vectors_config = qm.VectorParams(
                size=self.embedding_dim, distance=qm.Distance.COSINE
            )
            try:
                if self._client.collection_exists(collection_name=self.collection):
                    self._client.delete_collection(collection_name=self.collection)
                self._client.create_collection(
                    collection_name=self.collection, vectors_config=vectors_config
                )
            except Exception as e:
                raise VectorStoreError(f"failed to recreate qdrant collection: {e}") from e

        await _to_thread(_do)
        log.warning(
            "Qdrant collection %s was reset at dim=%s (all vectors discarded)",
            self.collection, self.embedding_dim,
        )

    async def count(self) -> int:
        def _do():
            res = self._client.count(collection_name=self.collection, exact=True)
            return int(res.count)

        return await _to_thread(_do)

    async def dimension(self) -> Optional[int]:
        """The size the collection's unnamed vector was created with."""

        def _do() -> Optional[int]:
            try:
                info = self._client.get_collection(collection_name=self.collection)
                vectors = getattr(info.config.params, "vectors", None)
                # Single unnamed vector → VectorParams.size; named vectors would
                # be a dict — this app only ever creates the unnamed kind.
                size = getattr(vectors, "size", None)
                return int(size) if size else None
            except Exception as e:
                log.debug("could not read qdrant collection dimension: %s", e)
                return None

        return await _to_thread(_do)

    async def aclose(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
