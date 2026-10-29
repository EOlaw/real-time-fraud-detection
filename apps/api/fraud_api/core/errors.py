"""Structured error responses: {"error": {"code", "message", "correlation_id", "details"?}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc
from starlette.exceptions import HTTPException as StarletteHTTPException

from fraud_api.core.logging import correlation_id_var, get_logger

log = get_logger(__name__)


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message, self.headers = (
            status_code,
            code,
            message,
            headers,
        )
        self.details = details


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "correlation_id": correlation_id_var.get(),
    }
    if details is not None:
        err["details"] = details
    return {"error": err}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            _body(exc.code, exc.message, exc.details), exc.status_code, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo field locations and messages, never the submitted values.
        details = [
            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        return JSONResponse(_body("validation_error", "Request validation failed", details), 422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(_body("http_error", str(exc.detail)), exc.status_code)

    @app.exception_handler(sa_exc.SQLAlchemyError)
    async def _database_error(_: Request, exc: Exception) -> JSONResponse:
        # Every database error that reaches the API boundary is counted once, here. Pool
        # exhaustion (overload) and lost connections are 503 + Retry-After: nothing was committed
        # and every write path is idempotent, so the client retries the same request.
        from fraud_api.observability.metrics import DB_ERRORS

        DB_ERRORS.labels("api").inc()
        if isinstance(exc, (sa_exc.TimeoutError, sa_exc.OperationalError)):
            log.warning("database_unavailable", error_type=type(exc).__name__)
            return JSONResponse(
                _body("database_unavailable", "The database is unavailable or overloaded; retry"),
                503,
                headers={"Retry-After": "2"},
            )
        log.exception("database_error", error_type=type(exc).__name__)
        return JSONResponse(_body("internal_error", "Internal server error"), 500)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(_body("internal_error", "Internal server error"), 500)
