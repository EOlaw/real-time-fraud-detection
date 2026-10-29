"""FastAPI application factory.

Run locally:
    uv run uvicorn fraud_api.main:app --port 8000
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from fraud_api.api.routes import health, v1
from fraud_api.core.config import Settings, get_settings
from fraud_api.core.errors import install_error_handlers
from fraud_api.core.logging import configure_logging, correlation_id_var, get_logger
from fraud_api.db.session import make_engine, make_session_factory
from fraud_api.observability.endpoint import record_http
from fraud_api.observability.endpoint import router as metrics_router
from fraud_api.observability.metrics import MODEL_INFO
from fraud_api.registry.service import register_model as register_model  # re-export
from fraud_api.scoring.context import build_scoring_context

log = get_logger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.scoring = build_scoring_context(settings, session_factory)
        if app.state.scoring.model is not None:
            m = app.state.scoring.model
            MODEL_INFO.labels(m.name, m.version, app.state.scoring.policy.version).set(1)
        app.state.redis = None
        app.state.redis_async = None
        if settings.redis_url is not None:
            # Short timeouts: a slow or absent Redis must not stall synchronous scoring requests
            # (feed publishing from those paths is best effort).
            import redis
            import redis.asyncio as aioredis

            url = settings.redis_url.get_secret_value()
            app.state.redis = redis.Redis.from_url(
                url, decode_responses=True, socket_timeout=2.0, socket_connect_timeout=2.0
            )
            # SSE blocks on XREAD for up to one heartbeat interval, so the read timeout must be
            # longer than that (redis-py's async default of 5 s would abort idle feeds).
            app.state.redis_async = aioredis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=settings.sse_heartbeat_seconds + 10.0,
            )
        yield
        if app.state.redis is not None:
            app.state.redis.close()
            await app.state.redis_async.aclose()
        engine.dispose()

    app = FastAPI(
        title="Fraud Detection & Risk Intelligence API",
        version="0.1.0",
        description="Real-time fraud risk scoring. Decisions are sandboxed (simulated): they are "
        "not connected to any money movement.",
        lifespan=lifespan,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def correlation(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        cid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Request-ID"] = cid
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            correlation_id=cid,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return response

    app.middleware("http")(record_http)
    app.include_router(health)
    app.include_router(metrics_router)
    app.include_router(v1)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy module-level ``app`` for uvicorn, so importing this module has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
