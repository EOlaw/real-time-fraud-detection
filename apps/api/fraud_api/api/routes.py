from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text

from fraud_api.api.deps import (
    SessionDep,
    get_scoring_context,
    get_settings,
    require_roles,
)
from fraud_api.core.config import Settings
from fraud_api.core.errors import APIError
from fraud_api.core.logging import correlation_id_var
from fraud_api.core.security import Principal, Role, issue_token, verify_password
from fraud_api.db.models import AuditLog, User
from fraud_api.scoring.schemas import PredictionDetail, ScoreRequest, ScoreResponse
from fraud_api.scoring.service import ScoringContext, get_prediction, score_transaction

health = APIRouter(prefix="/health", tags=["health"])
v1 = APIRouter(prefix="/api/v1")


@health.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/ready")
def ready(request: Request, session: SessionDep) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    ctx: ScoringContext = request.app.state.scoring
    checks["model"] = f"ok ({ctx.model.name}@{ctx.model.version})" if ctx.model else "unavailable"
    checks["policy"] = ctx.policy.version
    ok = checks["database"] == "ok" and ctx.model is not None
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


@v1.post("/auth/token", response_model=TokenResponse, tags=["auth"])
def login(
    body: LoginRequest, session: SessionDep, settings: Annotated[Settings, Depends(get_settings)]
) -> TokenResponse:
    user = session.scalar(select(User).where(User.email == body.email.lower()))
    valid = verify_password(body.password, user.password_hash if user else None)
    if user is None or not valid or not user.is_active:
        session.add(
            AuditLog(
                organization_id=user.organization_id if user else None,
                actor_user_id=user.id if user else None,
                action="auth.login_failed",
                resource_type="user",
                details={},
                correlation_id=correlation_id_var.get(),
            )
        )
        session.commit()
        raise APIError(401, "invalid_credentials", "Invalid email or password")
    token, ttl = issue_token(settings, Principal(user.id, user.organization_id, Role(user.role)))
    session.add(
        AuditLog(
            organization_id=user.organization_id,
            actor_user_id=user.id,
            action="auth.login",
            resource_type="user",
            resource_id=str(user.id),
            details={},
            correlation_id=correlation_id_var.get(),
        )
    )
    session.commit()
    return TokenResponse(access_token=token, expires_in=ttl)


@v1.post(
    "/fraud/score",
    response_model=ScoreResponse,
    tags=["fraud"],
    responses={
        409: {"description": "transaction_id reused with a different payload"},
        503: {"description": "Scoring unavailable (FRAUD_FAILURE_MODE=reject)"},
    },
)
def score(
    body: ScoreRequest,
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    principal: Annotated[Principal, Depends(require_roles(Role.SERVICE, Role.ADMIN))],
) -> ScoreResponse:
    """Score one transaction event. Retries with the same ``transaction_id`` and identical payload
    return the original decision (``idempotent_replay=true``)."""
    return score_transaction(
        session, ctx, principal.organization_id, principal.user_id, body, correlation_id_var.get()
    )


@v1.get("/fraud/predictions/{assessment_id}", response_model=PredictionDetail, tags=["fraud"])
def prediction(
    assessment_id: uuid.UUID,
    session: SessionDep,
    principal: Annotated[
        Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.VIEWER, Role.SERVICE))
    ],
) -> PredictionDetail:
    result = get_prediction(session, principal.organization_id, assessment_id)
    if result is None:
        # Same response for "missing" and "belongs to another org": no cross-tenant probing.
        raise APIError(404, "not_found", "Prediction not found")
    return result
