"""Centralized exceptions and FastAPI exception handlers."""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for all application-level errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DocumentError(AppError):
    status_code = 400
    code = "document_error"


class VectorStoreError(AppError):
    status_code = 500
    code = "vector_store_error"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class EmbeddingError(AppError):
    status_code = 502
    code = "embedding_error"

    def __init__(
        self,
        message: str,
        *,
        details: Dict[str, Any] | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message, details=details)
        #: HTTP status the embedding *vendor* returned — NOT the status our own
        #: API answers with (that stays 502 via ``status_code``). Set only when
        #: the error wraps an HTTP response; drives ``is_fatal_http_status``.
        self.upstream_status = upstream_status


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class AuthError(AppError):
    """Not signed in, or the token is expired / forged / revoked.

    The admin console treats any 401 as "bounce to the login page", so this must
    stay 401 and must not be reused for "signed in but not allowed".
    """

    status_code = 401
    code = "unauthorized"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"


def is_fatal_http_status(exc: BaseException) -> bool:
    """True when retrying the identical request cannot possibly succeed.

    An upstream 4xx (except 429, which has its own wait-and-retry path) means
    the API key, payload or endpoint is wrong — the vendor's message should
    surface immediately instead of three backoff rounds failing the same way.
    """
    code = getattr(exc, "upstream_status", None)
    return isinstance(code, int) and code != 429 and 400 <= code < 500


def make_retry_predicate(*retry_types: type):
    """Build a tenacity predicate: retry these types, minus fatal 4xx.

    Shared by the LLM adapters and the embedding client. tenacity 9 dropped
    the ``~`` operator on retry conditions, hence the explicit predicate
    instead of composing ``retry_if_exception_type & ~...``.
    """

    def _predicate(exc: BaseException) -> bool:
        return isinstance(exc, retry_types) and not is_fatal_http_status(exc)

    return _predicate


def _payload(exc: AppError) -> Dict[str, Any]:
    return {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        log.warning("AppError [%s]: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=_payload(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail), "details": {}}},
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Internal server error", "details": {}}},
        )
