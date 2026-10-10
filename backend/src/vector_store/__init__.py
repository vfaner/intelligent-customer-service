"""Vector store adapters + factory."""
from .base import BaseVectorStore, ChunkRecord, VectorHit, VectorStoreError
from .chroma_store import ChromaStore
from .factory import build_vector_store, get_shared_vector_store
from .milvus_store import MilvusStore
from .qdrant_store import QdrantStore

__all__ = [
    "BaseVectorStore",
    "VectorStoreError",
    "VectorHit",
    "ChunkRecord",
    "ChromaStore",
    "QdrantStore",
    "MilvusStore",
    "build_vector_store",
    "get_shared_vector_store",
]
