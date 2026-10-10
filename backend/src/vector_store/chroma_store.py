"""ChromaDB-backed vector store.

Uses persistent local storage by default (``PersistentClient``) and runs
inside an ``asyncio.to_thread`` shim so the blocking Chroma API doesn't
stall the event loop. Cosine similarity is the default distance metric;
we convert to a 0..1 score by ``1 - distance``.
"""
from __future__ import annotations

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


class ChromaStore(BaseVectorStore):
    name = "chroma"

    def __init__(
        self,
        persist_dir: str | Path,
        collection: str,
        embedding_dim: int = 0,
    ) -> None:
        try:
            import chromadb  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise VectorStoreError("chromadb is not installed. `pip install chromadb`") from e

        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection
        self.embedding_dim = embedding_dim

        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        self._collection = self._client.get_or_create_collection(
            name=collection,
            metadata={"hnsw:space": "cosine"},
        )
        log.info("ChromaStore ready: dir=%s collection=%s", self.persist_dir, collection)

    @staticmethod
    def _distance_to_score(distance: float) -> float:
        # cosine distance in [0, 2] -> similarity in [-1, 1]; map to [0, 1]
        sim = 1.0 - float(distance)
        return max(0.0, min(1.0, (sim + 1.0) / 2.0))

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
        ids = [c.chunk_id for c in chunks]
        docs = [c.text for c in chunks]
        metas: List[Dict[str, Any]] = []
        for c in chunks:
            m = dict(c.metadata or {})
            m.setdefault("document_id", document_id)
            metas.append(m)

        def _do():
            self._collection.upsert(ids=ids, documents=docs, embeddings=vectors, metadatas=metas)
            return len(ids)

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

        def _do():
            kwargs: Dict[str, Any] = {"query_embeddings": [vector], "n_results": max(1, top_k)}
            if where:
                kwargs["where"] = where
            return self._collection.query(**kwargs)

        res = await _to_thread(_do)
        ids = (res.get("ids") or [[]])[0]
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]

        hits: List[VectorHit] = []
        for cid, doc, dist, meta in zip(ids, docs, dists, metas):
            score = self._distance_to_score(dist)
            if score < threshold:
                continue
            hits.append(VectorHit(chunk_id=cid, text=doc or "", score=score, metadata=meta or {}))
        return hits

    async def reset(self) -> None:
        """Drop the collection and recreate it empty.

        Chroma infers the vector dimension from the first upsert, so the
        recreated collection accepts the new embedding model's dimension
        without it being passed anywhere.
        """

        def _do():
            try:
                self._client.delete_collection(name=self.collection_name)
            except Exception as e:
                raise VectorStoreError(f"failed to drop collection {self.collection_name}: {e}") from e
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )

        await _to_thread(_do)
        log.warning("Chroma collection %s was reset (all vectors discarded)", self.collection_name)

    async def delete_document(self, document_id: str) -> int:
        def _do():
            existing = self._collection.get(where={"document_id": document_id})
            ids = existing.get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
            return len(ids)

        return await _to_thread(_do)

    async def count(self) -> int:
        def _do():
            return self._collection.count()

        return int(await _to_thread(_do))

    async def dimension(self) -> Optional[int]:
        """Read the dimension Chroma locked the collection to.

        Chroma stores this lazily on the first insert in its internal
        ``collections`` sqlite table and never exposes it through the client
        API — not even when the collection is empty, which is exactly the
        confusing case (0 vectors but every new-dimension insert is rejected).
        A separate short-lived read-only connection avoids interfering with
        Chroma's own one; any failure (older schema, locked file) → None.
        """
        import sqlite3

        def _do() -> Optional[int]:
            db = self.persist_dir / "chroma.sqlite3"
            if not db.exists():
                return None
            con = None
            try:
                con = sqlite3.connect(str(db), timeout=1.0)
                row = con.execute(
                    "SELECT dimension FROM collections WHERE name = ? LIMIT 1",
                    (self.collection_name,),
                ).fetchone()
            except Exception as e:  # schema changed, db locked, ...
                log.debug("could not read chroma collection dimension: %s", e)
                return None
            finally:
                if con is not None:
                    con.close()
            if not row or not row[0]:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None

        return await _to_thread(_do)

    async def aclose(self) -> None:
        # Chroma doesn't need an explicit close; we drop references.
        self._collection = None  # type: ignore[assignment]
        self._client = None  # type: ignore[assignment]
