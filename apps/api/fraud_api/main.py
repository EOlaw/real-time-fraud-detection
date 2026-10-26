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
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fraud_api.api.routes import health, v1
from fraud_api.core.config import Settings, get_settings
from fraud_api.core.errors import install_error_handlers
from fraud_api.core.logging import configure_logging, correlation_id_var, get_logger
from fraud_api.db.models import ModelVersion
from fraud_api.db.session import make_engine, make_session_factory
from fraud_api.scoring.policy import DecisionPolicy
from fraud_api.scoring.service import ScoringContext
from fraud_ml.models.artifact import ArtifactError, LoadedModel, load_artifact

log = get_logger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


def register_model(session: Session, model: LoadedModel) -> uuid.UUID:
    """Record the serving model. Re-using a name@version for different bytes is refused.

    Uses INSERT ... ON CONFLICT DO NOTHING so concurrent workers starting against a fresh database
    do not race on the unique (name, version) constraint.
    """
    session.execute(
        pg_insert(ModelVersion)
        .values(
            id=uuid.uuid4(),
            name=model.name,
            version=model.version,
            artifact_sha256=model.sha256,
            dataset_version=model.manifest.get("dataset_version"),
            manifest=model.manifest,
            status="active",
        )
        .on_conflict_do_nothing(constraint="uq_model_versions_name_version")
    )
    existing = session.scalars(
        select(ModelVersion).where(
            ModelVersion.name == model.name, ModelVersion.version == model.version
        )
    ).one()
    if existing.artifact_sha256 != model.sha256:
        session.rollback()
        raise ArtifactError(
            f"{model.name}@{model.version} is registered with a different "
            "artifact hash; bump the model version instead of overwriting"
        )
    existing.status = "active"
    session.commit()
    return existing.id


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)
        policy = DecisionPolicy.load(settings.policy_path)  # invalid policy: fail fast
        model: LoadedModel | None
        model_version_id: uuid.UUID | None = None
        try:
            model = load_artifact(settings.model_dir)
        except (ArtifactError, OSError) as exc:
            # Start, but report not-ready; scoring follows FRAUD_FAILURE_MODE.
            log.error("model_load_failed", error=str(exc), model_dir=str(settings.model_dir))
            model = None
        if model is not None:
            policy.check_model(model.name, model.version)  # mismatch: fail fast
            with session_factory() as session:
                model_version_id = register_model(session, model)
            log.info(
                "model_loaded",
                model=model.name,
                version=model.version,
                sha256=model.sha256[:12],
                policy=policy.version,
            )
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.scoring = ScoringContext(
            model=model,
            model_version_id=model_version_id,
            policy=policy,
            failure_mode=settings.failure_mode,
            max_history_rows=settings.max_history_rows,
        )
        yield
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

    app.include_router(health)
    app.include_router(v1)
    return app


def __getattr__(name: str) -> FastAPI:
    # Lazy module-level ``app`` for uvicorn, so importing this module has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
