"""Real-time scoring pipeline.

idempotency -> point-in-time history -> features -> model -> policy -> persist.

Persistence of the transaction, its assessment and the audit record happens in a single database
transaction, so a stored transaction always has exactly one assessment.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from fraud_api.core.config import FailureMode
from fraud_api.core.errors import APIError
from fraud_api.core.logging import get_logger
from fraud_api.db.models import AuditLog, ModelVersion, RiskAssessment, Transaction
from fraud_api.scoring.policy import DecisionOutcome, DecisionPolicy
from fraud_api.scoring.schemas import (
    PredictionDetail,
    ScoreRequest,
    ScoreResponse,
    utcnow,
)
from fraud_ml.domains.card_payments import derive_channel
from fraud_ml.features.online import HistoryEvent, compute_online_features, to_epoch_us
from fraud_ml.features.spec import MAX_LOOKBACK_US, NUMERIC_FEATURES, encode
from fraud_ml.models.artifact import LoadedModel

log = get_logger(__name__)


@dataclass
class ScoringContext:
    model: LoadedModel | None
    model_version_id: uuid.UUID | None
    policy: DecisionPolicy
    failure_mode: FailureMode
    max_history_rows: int


class HistoryLimitExceeded(RuntimeError):
    pass


def _fetch_history(
    session: Session, org_id: uuid.UUID, req: ScoreRequest, limit: int
) -> list[HistoryEvent]:
    """Point-in-time history: same org + customer, event_time in [t - 30d, t)."""
    t = req.event_time
    rows = session.execute(
        select(Transaction.event_time, Transaction.amount, Transaction.category)
        .where(
            Transaction.organization_id == org_id,
            Transaction.customer_id == req.customer_id,
            Transaction.event_time >= t - timedelta(microseconds=MAX_LOOKBACK_US),
            Transaction.event_time < t,
        )
        .limit(limit + 1)
    ).all()
    if len(rows) > limit:
        # Never silently truncate: velocity features would be wrong.
        raise HistoryLimitExceeded(f"customer history exceeds {limit} rows in lookback window")
    return [HistoryEvent(to_epoch_us(r.event_time), float(r.amount), r.category) for r in rows]


def _json_features(features: dict[str, float | str]) -> dict[str, float | str | None]:
    return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in features.items()}


def _to_response(
    txn: Transaction, ra: RiskAssessment, model: ModelVersion | None, replay: bool
) -> ScoreResponse:
    return ScoreResponse(
        assessment_id=ra.id,
        transaction_id=txn.external_id,
        status=ra.status,
        risk_score=ra.risk_score,
        risk_level=ra.risk_level,
        decision=ra.decision,
        decision_mode=ra.decision_mode,
        reasons=ra.reasons,
        model_name=model.name if model else None,
        model_version=model.version if model else None,
        policy_version=ra.policy_version,
        source=txn.source,
        processing_time_ms=ra.timings_ms.get("total", float("nan")),
        scored_at=ra.scored_at,
        idempotent_replay=replay,
    )


def _existing(
    session: Session, org_id: uuid.UUID, req: ScoreRequest, payload_hash: str
) -> ScoreResponse | None:
    row = session.execute(
        select(Transaction, RiskAssessment, ModelVersion)
        .outerjoin(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .outerjoin(ModelVersion, ModelVersion.id == RiskAssessment.model_version_id)
        .where(Transaction.organization_id == org_id, Transaction.external_id == req.transaction_id)
    ).first()
    if row is None:
        return None
    txn, ra, mv = row
    if ra is None:
        # Imported history (backfill/replay file import) is stored unscored; it is not re-scored
        # under the same id.
        raise APIError(
            409,
            "transaction_exists_unscored",
            "transaction_id already exists as imported history and was not scored",
        )
    if txn.payload_sha256 != payload_hash:
        raise APIError(
            409,
            "idempotency_conflict",
            "transaction_id was already submitted with a different payload",
        )
    return _to_response(txn, ra, mv, replay=True)


def score_transaction(
    session: Session,
    ctx: ScoringContext,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    req: ScoreRequest,
    correlation_id: str | None,
) -> ScoreResponse:
    started = time.perf_counter()
    payload_hash = req.payload_sha256()
    if (prior := _existing(session, org_id, req, payload_hash)) is not None:
        return prior

    channel = derive_channel(req.category)
    timings: dict[str, float] = {}
    features: dict[str, float | str] | None = None
    score: float | None = None
    outcome: DecisionOutcome
    try:
        if ctx.model is None:
            raise RuntimeError("model_unavailable")
        t0 = time.perf_counter()
        history = _fetch_history(session, org_id, req, ctx.max_history_rows)
        features = compute_online_features(
            req.event_time, float(req.amount), req.category, channel, history
        )
        timings["features"] = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        x = encode(
            {n: [float(features[n])] for n in NUMERIC_FEATURES},
            {"category": [req.category], "channel": [channel]},
        )
        score = float(ctx.model.predict_proba(x)[0])
        timings["inference"] = (time.perf_counter() - t1) * 1000
        outcome = ctx.policy.decide(score)
    except SQLAlchemyError:
        # Database failures are infrastructure errors, not degraded scores: nothing can be
        # persisted, so the request fails (5xx) and the caller retries with the same id.
        raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        log.warning("scoring_failed", reason=reason, failure_mode=ctx.failure_mode.value)
        if ctx.failure_mode is FailureMode.REJECT:
            raise APIError(
                503,
                "scoring_unavailable",
                "Risk scoring is unavailable; apply caller fallback policy",
                headers={"Retry-After": "5"},
            ) from exc
        score, outcome = None, ctx.policy.on_failure(reason)

    txn = Transaction(
        organization_id=org_id,
        external_id=req.transaction_id,
        customer_id=req.customer_id,
        merchant_id=req.merchant_id,
        amount=req.amount,
        currency=req.currency,
        category=req.category,
        channel=channel,
        region=req.region,
        event_time=req.event_time,
        source=req.source.value,
        schema_version=req.schema_version,
        payload_sha256=payload_hash,
    )
    session.add(txn)
    try:
        session.flush()  # surfaces a concurrent duplicate before we write the assessment
    except IntegrityError:
        session.rollback()
        prior = _existing(session, org_id, req, payload_hash)
        if prior is None:
            raise
        return prior

    # Validation -> decision, excluding the final commit. End-to-end HTTP latency is measured
    # separately by the load tests.
    timings["total"] = (time.perf_counter() - started) * 1000
    ra = RiskAssessment(
        organization_id=org_id,
        transaction_id=txn.id,
        model_version_id=ctx.model_version_id if score is not None else None,
        status="scored" if score is not None else "degraded",
        risk_score=score,
        risk_level=outcome.risk_level.value,
        decision=outcome.decision.value,
        decision_mode=ctx.policy.mode,
        reasons=outcome.reasons,
        policy_version=ctx.policy.version,
        features=_json_features(features) if features is not None else None,
        timings_ms=timings,
        correlation_id=correlation_id,
        scored_at=utcnow(),
    )
    session.add(ra)
    session.flush()
    session.add(
        AuditLog(
            organization_id=org_id,
            actor_user_id=actor_id,
            action="fraud.score",
            resource_type="risk_assessment",
            resource_id=str(ra.id),
            details={"decision": ra.decision, "status": ra.status, "source": txn.source},
            correlation_id=correlation_id,
        )
    )
    session.commit()
    mv = session.get(ModelVersion, ctx.model_version_id) if ra.model_version_id else None
    return _to_response(txn, ra, mv, replay=False)


def get_prediction(
    session: Session, org_id: uuid.UUID, assessment_id: uuid.UUID
) -> PredictionDetail | None:
    row = session.execute(
        select(Transaction, RiskAssessment, ModelVersion)
        .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .outerjoin(ModelVersion, ModelVersion.id == RiskAssessment.model_version_id)
        .where(RiskAssessment.id == assessment_id, RiskAssessment.organization_id == org_id)
    ).first()
    if row is None:
        return None
    txn, ra, mv = row
    base = _to_response(txn, ra, mv, replay=False)
    return PredictionDetail(**base.model_dump(), features=ra.features, timings_ms=ra.timings_ms)
