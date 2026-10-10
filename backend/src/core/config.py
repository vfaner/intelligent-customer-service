"""Global application settings, loaded from environment / .env.

Each section is grouped via `pydantic_settings.BaseSettings` so a single
``Settings()`` instance is the single source of truth for everything that
needs configuration.
"""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List, Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _version_from_pyproject() -> str:
    """Read the running version out of backend/pyproject.toml, as ``vX.Y.Z``.

    The version describes the *code*, not the deployment, so it must travel
    with the code. It used to default to a hardcoded string that only
    APP_VERSION in .env could override — and .env is gitignored, so a fresh
    ``git pull`` never changed it. GET /api/admin/version then compared a
    stale number against the repo's latest tag and reported an update that
    was already installed, forever. Reading pyproject.toml makes bumping the
    version a one-line change that ships with the release.

    The ``v`` is added here rather than stored in pyproject.toml: that field
    is a PEP 440 package version, where a leading ``v`` is non-canonical and
    gets stripped when building a wheel — the two spellings would drift apart
    again. Git tags are ``vX.Y.Z``, and the update dialog shows current and
    latest side by side, so one consistent spelling reaches the UI from here.
    Comparison is unaffected either way: _parse_version() ignores the prefix.

    Falls back to "0.0.0" when the file isn't there (installed as a wheel, or
    a partial copy): an unknown version should look old rather than break boot.
    """
    raw = ""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        with pyproject.open("rb") as fh:
            raw = str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        # No source tree next to us — try installed package metadata instead.
        try:
            from importlib.metadata import PackageNotFoundError
            from importlib.metadata import version as _pkg_version

            raw = _pkg_version("intelligent-customer-service")
        except (PackageNotFoundError, ImportError):
            return "0.0.0"

    raw = raw.strip()
    return raw if raw.lower().startswith("v") else f"v{raw}"


class AppSettings(BaseSettings):
    name: str = "IntelligentCustomerService"
    env: Literal["development", "production", "test"] = "development"
    # Fail-safe default: debug=True makes Starlette render tracebacks over the
    # project's sanitized 500 handler, leaking paths and locals. Local .env
    # files set APP_DEBUG=true explicitly (see .env.example).
    debug: bool = False
    secret_key: str = "change-me-in-production"
    host: str = "0.0.0.0"
    port: int = 8000
    upload_dir: str = "./uploads"
    max_upload_mb: int = 20
    # Current release, compared against the repo's latest tag by
    # GET /api/admin/version. Single source of truth is pyproject.toml —
    # bump it there and nowhere else. APP_VERSION can still override it for
    # odd deployments, but you shouldn't need to set it.
    version: str = Field(default_factory=_version_from_pyproject)
    # "owner/repo" on GitHub, used by GET /api/admin/version. Set to empty to
    # disable the update check — the button then explains it's unconfigured
    # instead of failing silently.
    github_repo: str = "vfaner/intelligent-customer-service"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")


class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///./data/app.db"
    echo: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="DATABASE_", extra="ignore")


class RedisSettings(BaseSettings):
    url: str = ""
    enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_prefix="REDIS_", extra="ignore")


class VectorDBSettings(BaseSettings):
    provider: Literal["chroma", "qdrant", "milvus"] = "chroma"
    host: str = "localhost"
    port: int = 6333
    collection: str = "knowledge_base"
    embedding_dim: int = 1024
    chroma_persist_dir: str = "./data/chroma_db"
    # Milvus: leave `milvus_uri` as a local .db path for Milvus Lite
    # (zero-install), or set it to http://host:port for a real server.
    milvus_uri: str = "./data/milvus.db"
    milvus_port: int = 19530
    milvus_token: str = ""
    milvus_db_name: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="VECTOR_DB_", extra="ignore")


class LLMSettings(BaseSettings):
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.3
    max_tokens: int = 2048
    protocol: Literal["openai", "anthropic"] = "openai"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLM_", extra="ignore")


class EmbeddingSettings(BaseSettings):
    provider: Literal["openai_compatible", "sentence_transformer"] = "openai_compatible"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "text-embedding-3-small"
    dim: int = 1024

    model_config = SettingsConfigDict(env_file=".env", env_prefix="EMBEDDING_", extra="ignore")


class RAGSettings(BaseSettings):
    top_k: int = 5
    similarity_threshold: float = 0.5
    chunk_size: int = 500
    chunk_overlap: int = 100
    max_context_chars: int = 8000
    # structure = heading/Q&A aware (default) · window = fixed-size sliding
    chunk_strategy: Literal["structure", "window"] = "structure"
    # Prefix each chunk with its heading path so a retrieved fragment keeps
    # the context it came from.
    chunk_prefix_heading: bool = True
    # When retrieval finds nothing relevant: False (default) → refuse, which
    # guarantees every answer is traceable to your documents. True → let the
    # model answer from its own knowledge, which is friendlier but can be wrong
    # or outdated, and the user can't tell which is which.
    allow_model_knowledge: bool = False
    # Score above which retrieved chunks are considered a real answer to the
    # question (only consulted when allow_model_knowledge is on).
    #
    # `similarity_threshold` has to stay low (~0.5) or short queries like
    # "登录页" retrieve nothing at all. But with CJK embeddings even unrelated
    # text lands around 0.65-0.71, so "we got hits" ≠ "we got answers".
    # Measured on this corpus: on-topic questions score 0.81-0.91, off-topic
    # 0.65-0.71 — a clean gap, so 0.76 sits in the middle.
    relevance_threshold: float = 0.76

    model_config = SettingsConfigDict(env_file=".env", env_prefix="RAG_", extra="ignore")


class AuthSettings(BaseSettings):
    """Admin-console login.

    ``enabled=False`` turns every guard off — intended for a throwaway demo on a
    private network, never for anything reachable from the internet. It exists
    because "I locked myself out of my own laptop install" is otherwise a
    database-editing exercise.

    Tokens are signed with ``APP_SECRET_KEY``. Leave that at its default and
    anyone who reads this repo can forge one, so ``main.py`` logs a warning at
    boot when the default is still in place outside development.
    """

    enabled: bool = True
    # 7 days. Long enough that day-to-day use never hits a login form; short
    # enough that a stolen token isn't valid forever.
    token_ttl_hours: int = 168
    cookie_name: str = "cs_admin_token"
    # Seeded on first boot when the user table is empty. Changing these later
    # does NOT rename or re-password an existing account — use the admin
    # console, or scripts/reset_admin_password.py.
    default_username: str = "admin"
    default_password: str = "123456"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="AUTH_", extra="ignore")


class CORSSettings(BaseSettings):
    # NoDecode: leave the raw env string alone so we can split on comma below.
    origins: Annotated[List[str], NoDecode] = Field(default_factory=lambda: ["*"])

    model_config = SettingsConfigDict(env_file=".env", env_prefix="CORS_", extra="ignore")

    @field_validator("origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if v is None or v == "":
            return ["*"]
        if isinstance(v, str):
            v = v.strip()
            if v == "*":
                return ["*"]
            # Try JSON first (so `["a","b"]` works), fall back to CSV.
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [item.strip() for item in v.split(",") if item.strip()]
        if isinstance(v, list):
            return v
        return ["*"]


class Settings:
    """Composite settings — one instance, lazy-instantiated."""

    def __init__(self) -> None:
        self.app = AppSettings()
        self.database = DatabaseSettings()
        self.redis = RedisSettings()
        self.vector_db = VectorDBSettings()
        self.llm = LLMSettings()
        self.embedding = EmbeddingSettings()
        self.rag = RAGSettings()
        self.auth = AuthSettings()
        self.cors = CORSSettings()

    # Convenience pass-throughs (used a lot in services)
    @property
    def upload_dir(self) -> Path:
        return Path(self.app.upload_dir).resolve()

    @property
    def chroma_persist_dir(self) -> Path:
        return Path(self.vector_db.chroma_persist_dir).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — used as FastAPI dependency."""
    return Settings()
