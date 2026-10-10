"""Milvus-backed vector store.

Supports three deployment modes, all through the same ``MilvusClient``:

  * **Milvus Lite** — pass a local ``.db`` file path as ``uri``
    (e.g. ``./data/milvus.db``). Zero-install, good for development.
  * **Milvus Standalone / Cluster** — pass ``http://host:port``
    (default ``http://localhost:19530``).
  * **Zilliz Cloud** — pass the cluster endpoint plus ``token``.

Chunk ids are the same ``{document_id}:{index}`` strings used by the other
adapters, so the collection is created with ``id_type="string"``. Metadata
lives in a JSON ``meta`` field; ``document_id`` is stored as its own
scalar field so deletes and filters can use a boolean expression.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    BaseVectorStore,
    ChunkRecord,
    VectorHit,
    VectorStoreError,
    to_thread as _to_thread,
)

log = logging.getLogger(__name__)

# Milvus VARCHAR fields need an explicit max length.
_ID_MAX_LEN = 512
_DOC_ID_MAX_LEN = 128
_TEXT_MAX_LEN = 65535
_META_MAX_LEN = 65535


def _escape(value: str) -> str:
    """Escape a string for use inside a Milvus filter expression."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusStore(BaseVectorStore):
    name = "milvus"

    def __init__(
        self,
        collection: str = "knowledge_base",
        embedding_dim: int = 0,
        *,
        uri: str = "",
        host: str = "localhost",
        port: int = 19530,
        token: str = "",
        db_name: str = "",
    ) -> None:
        try:
            from pymilvus import DataType, MilvusClient  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise VectorStoreError(
                "pymilvus is not installed. `pip install pymilvus`"
            ) from e

        if embedding_dim <= 0:
            raise VectorStoreError(
                "Milvus requires a fixed vector dimension. "
                "Set VECTOR_DB_EMBEDDING_DIM (and EMBEDDING_DIM) to match your model."
            )

        self.collection = collection
        self.embedding_dim = embedding_dim
        self._DataType = DataType

        resolved_uri = self._resolve_uri(uri, host, port)
        client_kwargs: Dict[str, Any] = {"uri": resolved_uri}
        if token:
            client_kwargs["token"] = token
        if db_name:
            client_kwargs["db_name"] = db_name

        try:
            self._client = MilvusClient(**client_kwargs)
        except Exception as e:
            raise VectorStoreError(f"Cannot connect to Milvus at {resolved_uri}: {e}") from e

        self._ensure_collection()
        log.info("MilvusStore ready: uri=%s collection=%s dim=%s",
                 resolved_uri, collection, embedding_dim)

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_uri(uri: str, host: str, port: int) -> str:
        """Build the client URI.

        A bare filesystem path (``./data/milvus.db``) selects Milvus Lite;
        anything else is treated as a server endpoint.
        """
        if uri:
            u = uri.strip()
            if u.endswith(".db") or u.startswith("/") or u.startswith("./"):
                p = Path(u).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                return str(p)
            return u
        return f"http://{host}:{port}"

    def _ensure_collection(self) -> None:
        """Create the collection + index if it doesn't exist yet."""
        DataType = self._DataType
        try:
            if self._client.has_collection(collection_name=self.collection):
                # Make sure it's loaded so searches work right after boot.
                try:
                    self._client.load_collection(collection_name=self.collection)
                except Exception:
                    pass
                return
        except Exception as e:
            raise VectorStoreError(f"Milvus has_collection failed: {e}") from e

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=_ID_MAX_LEN)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.embedding_dim)
        schema.add_field("document_id", DataType.VARCHAR, max_length=_DOC_ID_MAX_LEN)
        schema.add_field("text", DataType.VARCHAR, max_length=_TEXT_MAX_LEN)
        schema.add_field("meta", DataType.VARCHAR, max_length=_META_MAX_LEN)

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )

        try:
            self._client.create_collection(
                collection_name=self.collection,
                schema=schema,
                index_params=index_params,
            )
            self._client.load_collection(collection_name=self.collection)
        except Exception as e:
            raise VectorStoreError(f"Milvus create_collection failed: {e}") from e
        log.info("Created Milvus collection %s (dim=%s)", self.collection, self.embedding_dim)

    @staticmethod
    def _score_from_cosine(raw: float) -> float:
        # Milvus COSINE returns similarity in [-1, 1]; map to [0, 1] so the
        # threshold semantics match the Chroma / Qdrant adapters.
        return max(0.0, min(1.0, (float(raw) + 1.0) / 2.0))

    # ------------------------------------------------------------------
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

        rows: List[Dict[str, Any]] = []
        for c, v in zip(chunks, vectors):
            if len(v) != self.embedding_dim:
                raise VectorStoreError(
                    f"vector dim mismatch: got {len(v)}, collection expects {self.embedding_dim}"
                )
            meta = dict(c.metadata or {})
            meta.setdefault("document_id", document_id)
            rows.append(
                {
                    "id": c.chunk_id,
                    "vector": [float(x) for x in v],
                    "document_id": document_id,
                    "text": c.text,
                    "meta": json.dumps(meta, ensure_ascii=False),
                }
            )

        def _do():
            self._client.upsert(collection_name=self.collection, data=rows)
            return len(rows)

        try:
            return await _to_thread(_do)
        except Exception as e:
            raise VectorStoreError(f"Milvus upsert failed: {e}") from e

    async def query(
        self,
        vector: List[float],
        top_k: int = 5,
        threshold: float = 0.0,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[VectorHit]:
        if not vector:
            return []

        expr = ""
        if where and where.get("document_id"):
            expr = f'document_id == "{_escape(str(where["document_id"]))}"'

        def _do():
            return self._client.search(
                collection_name=self.collection,
                data=[[float(x) for x in vector]],
                limit=max(1, top_k),
                filter=expr,
                output_fields=["id", "document_id", "text", "meta"],
                search_params={"metric_type": "COSINE"},
            )

        try:
            results = await _to_thread(_do)
        except Exception as e:
            raise VectorStoreError(f"Milvus search failed: {e}") from e

        hits: List[VectorHit] = []
        for group in results or []:
            for r in group:
                score = self._score_from_cosine(r.get("distance", 0.0))
                if score < threshold:
                    continue
                entity = r.get("entity") or r
                raw_meta = entity.get("meta") or "{}"
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta)
                except Exception:
                    meta = {}
                meta.setdefault("document_id", entity.get("document_id", ""))
                hits.append(
                    VectorHit(
                        chunk_id=str(entity.get("id") or r.get("id") or ""),
                        text=entity.get("text", "") or "",
                        score=score,
                        metadata=meta,
                    )
                )
        return hits

    async def delete_document(self, document_id: str) -> int:
        expr = f'document_id == "{_escape(document_id)}"'

        def _count():
            rows = self._client.query(
                collection_name=self.collection,
                filter=expr,
                output_fields=["id"],
                limit=16384,
            )
            return len(rows or [])

        def _do():
            self._client.delete(collection_name=self.collection, filter=expr)

        try:
            n = await _to_thread(_count)
            if n == 0:
                return 0
            await _to_thread(_do)
            return n
        except Exception as e:
            raise VectorStoreError(f"Milvus delete failed: {e}") from e

    async def reset(self) -> None:
        """Drop and recreate the collection at the configured dimension."""

        def _do():
            try:
                if self._client.has_collection(collection_name=self.collection):
                    try:
                        self._client.release_collection(collection_name=self.collection)
                    except Exception:
                        pass
                    self._client.drop_collection(collection_name=self.collection)
            except Exception as e:
                raise VectorStoreError(f"Milvus drop collection failed: {e}") from e
            self._ensure_collection()

        await _to_thread(_do)
        log.warning(
            "Milvus collection %s was reset at dim=%s (all vectors discarded)",
            self.collection, self.embedding_dim,
        )

    async def count(self) -> int:
        def _do():
            stats = self._client.get_collection_stats(collection_name=self.collection)
            # Key is "row_count" on both Lite and server builds.
            return int(stats.get("row_count", 0) or 0)

        try:
            return await _to_thread(_do)
        except Exception as e:
            log.warning("Milvus count failed: %s", e)
            return 0

    async def dimension(self) -> Optional[int]:
        """The FLOAT_VECTOR field's dim of the existing collection."""

        def _do() -> Optional[int]:
            try:
                desc = self._client.describe_collection(
                    collection_name=self.collection
                )
            except Exception as e:
                log.debug("could not read milvus collection dimension: %s", e)
                return None
            for f in desc.get("fields", []):
                if f.get("name") == "vector" or str(f.get("type", "")).endswith("FLOAT_VECTOR"):
                    raw = (f.get("params") or {}).get("dim")
                    try:
                        return int(raw) if raw is not None else None
                    except (TypeError, ValueError):
                        return None
            return None

        return await _to_thread(_do)

    async def aclose(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
