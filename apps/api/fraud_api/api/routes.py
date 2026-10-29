from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import redis
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text

from fraud_api.api.deps import (
    SessionDep,
    get_redis,
    get_redis_async,
    get_scoring_context,
    get_settings,
    get_stream_keys,
    require_roles,
)
from fraud_api.cases.schemas import (
    Assignee,
    AssignRequest,
    CaseCreate,
    CaseDetail,
    CaseNoteOut,
    CaseOpened,
    CasePage,
    CaseStatus,
    LabelCreate,
    LabelHistory,
    LabelOut,
    NoteCreate,
    ResolveRequest,
    Severity,
    StatusRequest,
)
from fraud_api.cases.service import (
    AssignedFilter,
    add_note,
    assign_case,
    assignees,
    case_detail,
    change_status,
    list_cases,
    open_case,
    resolve_case,
)
from fraud_api.cases.service import SortKey as CaseSortKey
from fraud_api.core.config import Settings
from fraud_api.core.errors import APIError
from fraud_api.core.logging import correlation_id_var
from fraud_api.core.security import Principal, Role, issue_token, verify_password
from fraud_api.db.models import AuditLog, User
from fraud_api.insights.analytics import (
    Window,
    latency,
    make_window,
    overview,
    rule_hits,
    volume,
)
from fraud_api.insights.analytics_schemas import (
    Basis,
    Bucket,
    LatencyOut,
    OverviewOut,
    RuleHitsOut,
    Source,
    VolumeOut,
)
from fraud_api.insights.schemas import AssessmentPage, DistributionsResponse, Explanation
from fraud_api.insights.service import (
    SortKey,
    explain_assessment,
    feature_distributions,
    list_assessments,
)
from fraud_api.labels.metrics import (
    CaseStats,
    LabelStats,
    Performance,
    case_stats,
    label_stats,
    performance,
)
from fraud_api.labels.service import create_label, label_history
from fraud_api.monitoring.api import RunDetail, RunOut, list_runs, run_detail
from fraud_api.registry.schemas import ModelMetrics, ModelsResponse, ModelVersionDetail
from fraud_api.registry.service import get_metrics, get_version, list_versions
from fraud_api.rules.grammar import grammar_description
from fraud_api.rules.schemas import (
    RULE_KEY_PATTERN,
    ActivationOut,
    ActivationRequest,
    RuleCreate,
    RuleDetailOut,
    RuleListOut,
    RuleVersionOut,
)
from fraud_api.rules.service import (
    activate_rule,
    create_rule,
    deactivate_rule,
    get_rule,
    get_rule_version,
    list_rules,
)
from fraud_api.scoring.batch import BatchScoreRequest, BatchScoreResponse, score_batch
from fraud_api.scoring.schemas import (
    SCORE_REQUEST_EXAMPLE,
    PredictionDetail,
    ScoreRequest,
    ScoreResponse,
)
from fraud_api.scoring.service import ScoringContext, get_prediction, score_transaction
from fraud_api.stream.feed import feed_payload, publish_best_effort
from fraud_api.stream.ingest import IngestRequest, IngestResponse, ingest_events
from fraud_api.stream.keys import StreamKeys
from fraud_api.stream.ops import stream_status
from fraud_api.stream.sse import CURSOR, assessment_events

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
    request: Request,
    body: ScoreRequest,
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    keys: Annotated[StreamKeys, Depends(get_stream_keys)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_roles(Role.SERVICE, Role.ADMIN))],
) -> ScoreResponse:
    """Score one transaction event. Retries with the same ``transaction_id`` and identical payload
    return the original decision (``idempotent_replay=true``)."""
    response = score_transaction(
        session, ctx, principal.organization_id, principal.user_id, body, correlation_id_var.get()
    )
    # Best effort: the assessment is already committed; a Redis problem must not fail the request.
    publish_best_effort(
        request.app.state.redis,
        keys,
        principal.organization_id,
        feed_payload(response, "api", body.event_time),
        settings.feed_max_len,
    )
    return response


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


ReaderDep = Annotated[Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.VIEWER))]


@v1.get("/fraud/assessments", response_model=AssessmentPage, tags=["insights"])
def assessments(
    session: SessionDep,
    principal: ReaderDep,
    q: Annotated[str | None, Query(max_length=128)] = None,
    sort: SortKey = "scored_at",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    source: Annotated[list[Source] | None, Query(description="Repeat to include several")] = None,
) -> AssessmentPage:
    """Scored transactions of the caller's organization (imported, unscored history excluded)."""
    return list_assessments(
        session,
        principal.organization_id,
        q,
        sort,
        order,
        limit,
        offset,
        list(source) if source else None,
    )


@v1.get(
    "/fraud/predictions/{assessment_id}/explanation",
    response_model=Explanation,
    tags=["insights"],
    responses={409: {"description": "Degraded, or scored by a model that is no longer served"}},
)
def explanation(
    assessment_id: uuid.UUID,
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    principal: ReaderDep,
    top_k: Annotated[int, Query(ge=1, le=19)] = 6,
) -> Explanation:
    """Exact TreeSHAP contributions (log-odds) recomputed from the persisted features."""
    return explain_assessment(session, ctx, principal.organization_id, assessment_id, top_k)


@v1.get("/fraud/insights/distributions", response_model=DistributionsResponse, tags=["insights"])
def distributions(
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    principal: ReaderDep,
    sample: Annotated[int, Query(ge=1, le=20_000)] = 5_000,
) -> DistributionsResponse:
    """Feature distributions over the most recent scored assessments, plus policy thresholds."""
    return feature_distributions(session, ctx, principal.organization_id, sample)


# ---------------------------------------------------------------- batch scoring (Phase 4)

_BATCH_EXAMPLE_ITEM_2 = {
    **SCORE_REQUEST_EXAMPLE,
    "transaction_id": "txn_000124",
    "amount": "912.00",
    "category": "shopping_net",
    "event_time": "2020-06-21T12:20:00Z",
}


@v1.post(
    "/fraud/batch",
    response_model=BatchScoreResponse,
    tags=["fraud"],
    responses={
        413: {"description": "More than FRAUD_MAX_BATCH_SIZE items (default 500)"},
        422: {"description": "Envelope invalid (missing/empty items); items are validated singly"},
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "two_events": {
                            "summary": "Two events for one customer",
                            "value": {"items": [SCORE_REQUEST_EXAMPLE, _BATCH_EXAMPLE_ITEM_2]},
                        }
                    }
                }
            }
        }
    },
)
def score_batch_endpoint(
    request: Request,
    keys: Annotated[StreamKeys, Depends(get_stream_keys)],
    body: BatchScoreRequest,
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_roles(Role.SERVICE, Role.ADMIN))],
) -> BatchScoreResponse:
    """Score up to ``FRAUD_MAX_BATCH_SIZE`` events with per-item outcomes.

    Items are validated individually, de-duplicated by ``transaction_id`` and processed in
    ``(event_time, transaction_id)`` order, each in its own database transaction through the same
    code path as ``POST /fraud/score``. Check every item's ``status``: only ``scored``,
    ``degraded`` and ``duplicate`` carry a decision, and ``degraded`` is never ``allow``.
    """
    response = score_batch(
        session,
        ctx,
        principal.organization_id,
        principal.user_id,
        body.items,
        correlation_id_var.get(),
        settings.max_batch_size,
    )
    for item in response.items:  # best-effort feed of persisted outcomes (not duplicates)
        if item.result is not None and item.status != "duplicate":
            event_time = ScoreRequest.model_validate(body.items[item.index]).event_time
            publish_best_effort(
                request.app.state.redis,
                keys,
                principal.organization_id,
                feed_payload(item.result, "batch", event_time),
                settings.feed_max_len,
            )
    return response


# ---------------------------------------------------------------- model metadata (Phase 4)

AnyRoleDep = Annotated[
    Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.VIEWER, Role.SERVICE))
]


@v1.get("/models", response_model=ModelsResponse, tags=["models"])
def models(
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    _: AnyRoleDep,
) -> ModelsResponse:
    """Registered model versions (read-only). ``status`` comes from the registry; ``serving`` marks
    the version loaded by the answering instance. Registering or listing never promotes."""
    versions = list_versions(session, ctx.model_version_id)
    serving = next((v for v in versions if v.serving), None)
    return ModelsResponse(serving=serving, versions=versions)


@v1.get(
    "/models/active",
    response_model=ModelVersionDetail,
    tags=["models"],
    responses={503: {"description": "No model loaded by this instance"}},
)
def active_model(
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    _: AnyRoleDep,
) -> ModelVersionDetail:
    """The model this instance is serving, with metadata read from the registry and manifest."""
    if ctx.model_version_id is None:
        raise APIError(503, "model_unavailable", "No model is loaded by this instance")
    detail = get_version(session, ctx.model_version_id, ctx.model_version_id)
    if detail is None:
        raise APIError(503, "model_unavailable", "Serving model is missing from the registry")
    return detail


@v1.get("/models/{model_version_id}", response_model=ModelVersionDetail, tags=["models"])
def model_version(
    model_version_id: uuid.UUID,
    session: SessionDep,
    ctx: Annotated[ScoringContext, Depends(get_scoring_context)],
    _: AnyRoleDep,
) -> ModelVersionDetail:
    detail = get_version(session, model_version_id, ctx.model_version_id)
    if detail is None:
        raise APIError(404, "not_found", "Model version not found")
    return detail


@v1.get("/models/{model_version_id}/metrics", response_model=ModelMetrics, tags=["models"])
def model_metrics(model_version_id: uuid.UUID, session: SessionDep, _: AnyRoleDep) -> ModelMetrics:
    """Validation and held-out test metrics recorded in the version's manifest at training time."""
    metrics = get_metrics(session, model_version_id)
    if metrics is None:
        raise APIError(404, "not_found", "Model version not found")
    return metrics


# ---------------------------------------------------------------- streaming (Phase 5)


@v1.post(
    "/events",
    response_model=IngestResponse,
    status_code=202,
    tags=["stream"],
    responses={
        413: {"description": "More than FRAUD_MAX_BATCH_SIZE events"},
        503: {"description": "Streaming not configured, Redis unavailable, or backpressure"},
    },
)
def ingest(
    body: IngestRequest,
    client: Annotated[redis.Redis, Depends(get_redis)],
    keys: Annotated[StreamKeys, Depends(get_stream_keys)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: Annotated[Principal, Depends(require_roles(Role.SERVICE, Role.ADMIN))],
) -> IngestResponse:
    """Queue events for asynchronous scoring (202 = durably queued, not yet scored).

    The tenant comes from the token. Events are routed to a shard by customer and scored in
    ingest order per shard; outcomes appear on ``GET /api/v1/stream/assessments``.
    """
    return ingest_events(
        client,
        keys,
        principal.organization_id,
        principal.user_id,
        body.items,
        correlation_id_var.get() or "",
        settings.max_batch_size,
        settings.stream_max_backlog,
    )


@v1.get(
    "/stream/assessments",
    tags=["stream"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Server-Sent Events of persisted assessments",
        }
    },
)
async def assessment_stream(
    request: Request,
    client: Annotated[aioredis.Redis, Depends(get_redis_async)],
    keys: Annotated[StreamKeys, Depends(get_stream_keys)],
    settings: Annotated[Settings, Depends(get_settings)],
    principal: AnyRoleDep,
    cursor: Annotated[str | None, Query(max_length=41)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=41)] = None,
    limit: Annotated[int | None, Query(ge=1, le=10_000)] = None,
    idle_timeout: Annotated[float | None, Query(gt=0, le=300)] = None,
) -> StreamingResponse:
    """Tenant-scoped live feed of persisted assessments (at-least-once; de-duplicate by
    ``assessment_id``). Resume with ``Last-Event-ID`` or ``cursor``."""
    start = last_event_id or cursor
    if start is not None and not CURSOR.match(start):
        raise APIError(400, "invalid_cursor", "Cursor must be a stream ID like 1700000000000-0")
    events = assessment_events(
        client,
        keys,
        principal.organization_id,
        start,
        limit,
        idle_timeout,
        settings.sse_heartbeat_seconds,
        settings.sse_max_connection_seconds,
        request.is_disconnected,
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        # no-transform: proxies (including the Next.js dev/prod server) must not compress or
        # otherwise buffer the event stream; gzip would hold events until a block fills.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@v1.get("/stream/status", tags=["stream"])
def stream_status_endpoint(
    client: Annotated[redis.Redis, Depends(get_redis)],
    keys: Annotated[StreamKeys, Depends(get_stream_keys)],
    _: Annotated[Principal, Depends(require_roles(Role.ADMIN))],
) -> dict[str, Any]:
    """Platform-wide stream health (backlog, pending, leases, dead letters). Admin only; contains
    no event data."""
    return stream_status(client, keys)


# ---------------------------------------------------------------- rules engine (Phase 6)

RuleAuthorDep = Annotated[Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST))]
RuleAdminDep = Annotated[Principal, Depends(require_roles(Role.ADMIN))]
RuleKeyPath = Annotated[str, Path(pattern=RULE_KEY_PATTERN)]


@v1.get("/rules/grammar", tags=["rules"])
def rules_grammar(_: ReaderDep) -> dict[str, Any]:
    """Fields, operators and limits of the rule condition language."""
    return grammar_description()


@v1.get("/rules", response_model=RuleListOut, tags=["rules"])
def rules_list(session: SessionDep, principal: ReaderDep) -> RuleListOut:
    """Rule keys of the caller's organization with their latest and active versions."""
    return list_rules(session, principal.organization_id)


@v1.post(
    "/rules",
    response_model=RuleVersionOut,
    status_code=201,
    tags=["rules"],
    responses={422: {"description": "invalid_rule: condition rejected by the grammar"}},
)
def rules_create(body: RuleCreate, session: SessionDep, principal: RuleAuthorDep) -> RuleVersionOut:
    """Create a new immutable version of ``rule_key`` (inactive until an admin activates it)."""
    return create_rule(
        session, principal.organization_id, principal.user_id, body, correlation_id_var.get()
    )


@v1.get("/rules/{rule_key}", response_model=RuleDetailOut, tags=["rules"])
def rules_get(rule_key: RuleKeyPath, session: SessionDep, principal: ReaderDep) -> RuleDetailOut:
    return get_rule(session, principal.organization_id, rule_key)


@v1.get("/rules/{rule_key}/versions/{version}", response_model=RuleVersionOut, tags=["rules"])
def rules_get_version(
    rule_key: RuleKeyPath,
    version: Annotated[int, Path(ge=1)],
    session: SessionDep,
    principal: ReaderDep,
) -> RuleVersionOut:
    return get_rule_version(session, principal.organization_id, rule_key, version)


@v1.post(
    "/rules/{rule_key}/versions/{version}/activate",
    response_model=ActivationOut,
    tags=["rules"],
    responses={409: {"description": "activation_conflict (guard failed) or too_many_active_rules"}},
)
def rules_activate(
    rule_key: RuleKeyPath,
    version: Annotated[int, Path(ge=1)],
    session: SessionDep,
    principal: RuleAdminDep,
    body: ActivationRequest | None = None,
) -> ActivationOut:
    """Make this version the active one for its key (replacing any other version), atomically
    with its audit record. Scoring requests that start afterwards use it."""
    return activate_rule(
        session,
        principal.organization_id,
        principal.user_id,
        rule_key,
        version,
        body.expected_active_version if body else None,
        correlation_id_var.get(),
    )


@v1.post(
    "/rules/{rule_key}/deactivate",
    response_model=ActivationOut,
    tags=["rules"],
    responses={409: {"description": "activation_conflict (guard failed)"}},
)
def rules_deactivate(
    rule_key: RuleKeyPath,
    session: SessionDep,
    principal: RuleAdminDep,
    body: ActivationRequest | None = None,
) -> ActivationOut:
    return deactivate_rule(
        session,
        principal.organization_id,
        principal.user_id,
        rule_key,
        body.expected_active_version if body else None,
        correlation_id_var.get(),
    )


# ---------------------------------------------------------------- analytics (Phase 6)


def analytics_window(
    start: Annotated[datetime, Query(description="Inclusive, ISO 8601 with offset")],
    end: Annotated[datetime, Query(description="Exclusive, ISO 8601 with offset")],
    basis: Basis = "processed",
    source: Annotated[list[Source] | None, Query(description="Repeat to include several")] = None,
) -> Window:
    return make_window(start, end, basis, source)


WindowDep = Annotated[Window, Depends(analytics_window)]
_WINDOW_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "invalid_window: naive, empty, reversed, too long or misaligned"}
}


@v1.get(
    "/fraud/insights/overview",
    response_model=OverviewOut,
    tags=["insights"],
    responses=_WINDOW_ERRORS,
)
def insights_overview(session: SessionDep, principal: ReaderDep, w: WindowDep) -> OverviewOut:
    """Volume, decision / policy-decision / risk-level mix, alert and rule-escalation rates."""
    return overview(session, principal.organization_id, w)


@v1.get(
    "/fraud/insights/volume", response_model=VolumeOut, tags=["insights"], responses=_WINDOW_ERRORS
)
def insights_volume(
    session: SessionDep, principal: ReaderDep, w: WindowDep, bucket: Bucket = "hour"
) -> VolumeOut:
    """Per-bucket transactions, assessments, alerts and decisions (UTC-aligned buckets, zeros
    included)."""
    return volume(session, principal.organization_id, w, bucket)


@v1.get(
    "/fraud/insights/rule-hits",
    response_model=RuleHitsOut,
    tags=["insights"],
    responses=_WINDOW_ERRORS,
)
def insights_rule_hits(session: SessionDep, principal: ReaderDep, w: WindowDep) -> RuleHitsOut:
    """Evaluations, matches, unknowns, errors and decision influence per rule version."""
    return rule_hits(session, principal.organization_id, w)


@v1.get(
    "/fraud/insights/latency",
    response_model=LatencyOut,
    tags=["insights"],
    responses=_WINDOW_ERRORS,
)
def insights_latency(session: SessionDep, principal: ReaderDep, w: WindowDep) -> LatencyOut:
    """Distribution of persisted server-side scoring time per assessment."""
    return latency(session, principal.organization_id, w)


# ---------------------------------------------------------------- cases and labels (Phase 8)

CaseReaderDep = Annotated[Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST, Role.VIEWER))]
CaseWorkerDep = Annotated[Principal, Depends(require_roles(Role.ADMIN, Role.ANALYST))]
CtxDep = Annotated[ScoringContext, Depends(get_scoring_context)]
_CASE_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"description": "Role or assignment does not allow this change"},
    404: {"description": "Case not found in the caller's organization"},
    409: {"description": "version_conflict (stale expected_version), invalid_transition, ..."},
    503: {"description": "database_unavailable: nothing was changed"},
}


@v1.post(
    "/cases",
    response_model=CaseOpened,
    tags=["cases"],
    responses={201: {"description": "Case opened"}, 200: {"description": "Case already existed"}},
)
def cases_open(
    body: CaseCreate, session: SessionDep, ctx: CtxDep, principal: CaseWorkerDep
) -> JSONResponse:
    """Open the investigation case for an assessment (one case per transaction; repeating the
    request returns the existing case with ``created=false``)."""
    result = open_case(session, principal, body, correlation_id_var.get(), ctx.model_version_id)
    return JSONResponse(result.model_dump(mode="json"), status_code=201 if result.created else 200)


@v1.get("/cases", response_model=CasePage, tags=["cases"])
def cases_list(
    session: SessionDep,
    principal: CaseReaderDep,
    status: Annotated[list[CaseStatus] | None, Query()] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    assigned: AssignedFilter = "any",
    source: Annotated[list[Source] | None, Query()] = None,
    sort: CaseSortKey = "opened_at",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CasePage:
    """The case queue of the caller's organization, filterable by status, severity, assignment
    and source."""
    return list_cases(
        session,
        principal,
        list(status) if status else None,
        list(severity) if severity else None,
        assigned,
        list(source) if source else None,
        sort,
        order,
        limit,
        offset,
    )


@v1.get("/cases/assignees", response_model=list[Assignee], tags=["cases"])
def cases_assignees(session: SessionDep, principal: CaseReaderDep) -> list[Assignee]:
    """Active analysts and admins of the caller's organization (possible assignees)."""
    return assignees(session, principal.organization_id)


@v1.get("/cases/{case_id}", response_model=CaseDetail, tags=["cases"], responses=_CASE_ERRORS)
def cases_get(
    case_id: uuid.UUID, session: SessionDep, ctx: CtxDep, principal: CaseReaderDep
) -> CaseDetail:
    return case_detail(session, principal, case_id, ctx.model_version_id)


@v1.post(
    "/cases/{case_id}/assign", response_model=CaseDetail, tags=["cases"], responses=_CASE_ERRORS
)
def cases_assign(
    case_id: uuid.UUID,
    body: AssignRequest,
    session: SessionDep,
    ctx: CtxDep,
    principal: CaseWorkerDep,
) -> CaseDetail:
    """Analysts claim an unassigned case or release their own; admins assign anyone."""
    return assign_case(
        session, principal, case_id, body, correlation_id_var.get(), ctx.model_version_id
    )


@v1.post(
    "/cases/{case_id}/status", response_model=CaseDetail, tags=["cases"], responses=_CASE_ERRORS
)
def cases_status(
    case_id: uuid.UUID,
    body: StatusRequest,
    session: SessionDep,
    ctx: CtxDep,
    principal: CaseWorkerDep,
) -> CaseDetail:
    """Move between open statuses (assignee or admin); reopening a closed case is admin-only."""
    return change_status(
        session, principal, case_id, body, correlation_id_var.get(), ctx.model_version_id
    )


@v1.post(
    "/cases/{case_id}/resolve", response_model=CaseDetail, tags=["cases"], responses=_CASE_ERRORS
)
def cases_resolve(
    case_id: uuid.UUID,
    body: ResolveRequest,
    session: SessionDep,
    ctx: CtxDep,
    principal: CaseWorkerDep,
) -> CaseDetail:
    """Close the case (sets ``resolved_at``). The outcome is the investigation result; a label is
    recorded only with ``record_label=true``. The assessment's decision is never changed."""
    return resolve_case(
        session, principal, case_id, body, correlation_id_var.get(), ctx.model_version_id
    )


@v1.post(
    "/cases/{case_id}/notes",
    response_model=CaseNoteOut,
    status_code=201,
    tags=["cases"],
    responses=_CASE_ERRORS,
)
def cases_note(
    case_id: uuid.UUID, body: NoteCreate, session: SessionDep, principal: CaseWorkerDep
) -> CaseNoteOut:
    """Append a note (notes are append-only and cannot be edited or deleted)."""
    return add_note(session, principal, case_id, body, correlation_id_var.get())


@v1.get("/labels", response_model=LabelHistory, tags=["labels"])
def labels_get(
    session: SessionDep,
    principal: CaseReaderDep,
    transaction_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> LabelHistory:
    """Complete label history of a transaction (oldest first) and its current label."""
    return label_history(session, principal.organization_id, transaction_id)


@v1.post(
    "/labels",
    response_model=LabelOut,
    status_code=201,
    tags=["labels"],
    responses={
        409: {"description": "label_conflict: supersedes_id is not the current label"},
        422: {"description": "invalid_label (timing, case mismatch, missing reason, ...)"},
    },
)
def labels_create(body: LabelCreate, session: SessionDep, principal: CaseWorkerDep) -> LabelOut:
    """Append a label. A transaction that already has a label needs a correction: set
    ``supersedes_id`` to the current label and give a reason. Nothing is overwritten."""
    return create_label(session, principal, body, correlation_id_var.get())


@v1.get(
    "/fraud/insights/cases", response_model=CaseStats, tags=["insights"], responses=_WINDOW_ERRORS
)
def insights_cases(session: SessionDep, principal: ReaderDep, w: WindowDep) -> CaseStats:
    """Cases opened and resolved in the window (by severity and outcome), time to resolution, and
    the open backlog now. ``basis`` does not apply (case timestamps are used)."""
    return case_stats(session, principal.organization_id, w)


@v1.get(
    "/fraud/insights/labels", response_model=LabelStats, tags=["insights"], responses=_WINDOW_ERRORS
)
def insights_labels(session: SessionDep, principal: ReaderDep, w: WindowDep) -> LabelStats:
    """Labels recorded in the window (by ``labeled_at``) by label, source and confidence."""
    return label_stats(session, principal.organization_id, w)


@v1.get(
    "/fraud/insights/performance",
    response_model=Performance,
    tags=["insights"],
    responses=_WINDOW_ERRORS,
)
def insights_performance(
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
    principal: ReaderDep,
    w: WindowDep,
    as_of: Annotated[datetime | None, Query(description="Default: now (UTC)")] = None,
    maturity_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    include_probable: bool = False,
) -> Performance:
    """Label-matured performance for transactions whose **event time** is in the window, using
    only labels known at ``as_of`` (``basis`` is ignored). Unlabeled, inconclusive and (by
    default) probable labels are excluded and counted; unlabeled signals are reported apart."""
    when = as_of or datetime.now(UTC)
    if when.tzinfo is None:
        raise APIError(400, "invalid_window", "as_of must include a timezone offset")
    return performance(
        session,
        principal.organization_id,
        w,
        when.astimezone(UTC),
        settings.label_maturity_days if maturity_days is None else maturity_days,
        include_probable,
    )


# ---------------------------------------------------------------- monitoring (Phase 9)


@v1.get("/monitoring/runs", response_model=list[RunOut], tags=["monitoring"])
def monitoring_runs(
    session: SessionDep,
    principal: ReaderDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RunOut]:
    """Monitoring runs for the caller's organization (newest window first). Runs are computed by
    the monitoring job (CLI); this endpoint only reads them."""
    return list_runs(session, principal.organization_id, limit)


@v1.get(
    "/monitoring/runs/{run_id}",
    response_model=RunDetail,
    tags=["monitoring"],
    responses={404: {"description": "Not found in the caller's organization"}},
)
def monitoring_run(run_id: uuid.UUID, session: SessionDep, principal: ReaderDep) -> RunDetail:
    """Every metric of a run: unlabeled distribution/operational signals and label-matured
    performance (``labeled``), each with sample sizes, thresholds and status."""
    return run_detail(session, principal.organization_id, run_id)
