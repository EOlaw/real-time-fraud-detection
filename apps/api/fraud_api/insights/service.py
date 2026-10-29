"""Read-only views over scored transactions for the analyst dashboard.

Nothing here changes a score or a decision. Explanations are recomputed from the features that
were persisted at scoring time, and only when the serving model is the model that produced the
score; otherwise the explanation would describe a different model and is refused.
"""

from __future__ import annotations

import math
import uuid
from itertools import pairwise
from typing import Final, Literal

import numpy as np
import pandas as pd
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from fraud_api.core.errors import APIError
from fraud_api.db.models import RiskAssessment, Transaction
from fraud_api.insights.schemas import (
    AssessmentPage,
    AssessmentRow,
    Contribution,
    Distribution,
    DistributionsResponse,
    Explanation,
)
from fraud_api.scoring.service import ScoringContext
from fraud_ml.domains.card_payments import MERCHANT_CATEGORIES
from fraud_ml.explain.treeshap import UNITS, explain_rows, tree_shap
from fraud_ml.features.spec import CATEGORICAL_FEATURES, NUMERIC_FEATURES, encode

SortKey = Literal["scored_at", "risk_score", "amount", "event_time"]
_SORT_COLUMNS: Final = {
    "scored_at": RiskAssessment.scored_at,
    "risk_score": RiskAssessment.risk_score,
    "amount": Transaction.amount,
    "event_time": Transaction.event_time,
}


def _row(txn: Transaction, ra: RiskAssessment) -> AssessmentRow:
    return AssessmentRow(
        assessment_id=ra.id,
        transaction_id=txn.external_id,
        customer_id=txn.customer_id,
        merchant_id=txn.merchant_id,
        amount=float(txn.amount),
        currency=txn.currency,
        category=txn.category,
        channel=txn.channel,
        event_time=txn.event_time,
        source=txn.source,
        status=ra.status,
        risk_score=ra.risk_score,
        risk_level=ra.risk_level,
        decision=ra.decision,
        policy_decision=ra.policy_decision,
        rules_status=ra.rules_status,
        scored_at=ra.scored_at,
    )


def list_assessments(
    session: Session,
    org_id: uuid.UUID,
    q: str | None,
    sort: SortKey,
    order: Literal["asc", "desc"],
    limit: int,
    offset: int,
    sources: list[str] | None = None,
) -> AssessmentPage:
    stmt = (
        select(Transaction, RiskAssessment)
        .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .where(RiskAssessment.organization_id == org_id)
    )
    if sources:
        stmt = stmt.where(Transaction.source.in_(sources))
    if q:
        # Prefix match on identifiers only; LIKE wildcards in user input are escaped.
        pattern = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(
                Transaction.external_id.like(pattern, escape="\\"),
                Transaction.customer_id.like(pattern, escape="\\"),
                Transaction.merchant_id.like(pattern, escape="\\"),
            )
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    column = _SORT_COLUMNS[sort]
    direction = desc(column).nulls_last() if order == "desc" else asc(column).nulls_first()
    rows = session.execute(
        stmt.order_by(direction, RiskAssessment.id).limit(limit).offset(offset)
    ).all()
    return AssessmentPage(
        total=total, limit=limit, offset=offset, items=[_row(t, r) for t, r in rows]
    )


def explain_assessment(
    session: Session, ctx: ScoringContext, org_id: uuid.UUID, assessment_id: uuid.UUID, top_k: int
) -> Explanation:
    row = session.execute(
        select(Transaction, RiskAssessment)
        .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .where(RiskAssessment.id == assessment_id, RiskAssessment.organization_id == org_id)
    ).first()
    if row is None:
        raise APIError(404, "not_found", "Prediction not found")
    txn, ra = row
    if ra.status != "scored" or ra.features is None:
        raise APIError(409, "not_explainable", "Degraded assessments have no score to explain")
    if ctx.model is None or ra.model_version_id != ctx.model_version_id:
        raise APIError(
            409,
            "model_mismatch",
            "This assessment was scored by a model that is not currently served; "
            "its explanation cannot be reproduced",
        )
    numeric = {
        n: [math.nan if ra.features.get(n) is None else float(ra.features[n])]
        for n in NUMERIC_FEATURES
    }
    x = encode(numeric, {"category": [txn.category], "channel": [txn.channel]})
    contribs = tree_shap(ctx.model.booster, x)
    frame_values: dict[str, float | str] = {n: v[0] for n, v in numeric.items()}
    frame_values.update({"category": txn.category, "channel": txn.channel})
    frame = pd.DataFrame([frame_values], columns=[*NUMERIC_FEATURES, *CATEGORICAL_FEATURES])
    detail = explain_rows(contribs, frame, [str(ra.id)], top_k=top_k)[0]
    top = detail["top_contributions"]
    scale = max((abs(c["contribution_log_odds"]) for c in top), default=0.0) or 1.0
    return Explanation(
        assessment_id=ra.id,
        units=UNITS,
        base_value_log_odds=detail["base_value_log_odds"],
        margin_log_odds=detail["margin_log_odds"],
        probability=detail["probability"],
        stored_risk_score=ra.risk_score,
        other_features_log_odds=detail["other_features_log_odds"],
        contributions=[
            Contribution(
                feature=c["feature"],
                value=c["value"],
                contribution_log_odds=c["contribution_log_odds"],
                normalized=c["contribution_log_odds"] / scale,
            )
            for c in top
        ],
    )


# Histogram specs: feature -> (title, bin edges, integer-valued). Bin edges are fixed so charts
# stay comparable as data arrives; they cover the observed range of the card data.
_NUMERIC_BINS: Final[dict[str, tuple[str, list[float], bool]]] = {
    "amount": ("Payment Amount, $", [0, 10, 25, 50, 75, 100, 150, 200, 300, 500, 1000, 1e9], False),
    "cust_txn_count_7d": (
        "# of TNX, Last 7 Days",
        [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 1e9],
        True,
    ),
    "cust_txn_count_30d": (
        "# of TNX, Last 30 Days",
        [0, 25, 50, 75, 100, 125, 150, 175, 200, 250, 1e9],
        True,
    ),
    "cust_same_category_count_30d": (
        "# of TNX in Category, Last 30 Days",
        [0, 1, 3, 5, 8, 11, 15, 20, 25, 30, 1e9],
        True,
    ),
    "hour_of_day": ("Hour of Day", [0, 3, 6, 9, 12, 15, 18, 21, 24], True),
}


def _bin_label(lo: float, hi: float, integer: bool) -> str:
    """Label for the bin [lo, hi). Integer bins show inclusive bounds (``[5, 10)`` -> ``5-9``)."""
    if hi >= 1e9:
        return f"{lo:g}+"
    if integer:
        return f"{lo:g}" if hi - lo == 1 else f"{lo:g}-{hi - 1:g}"
    return f"{lo:g}-{hi:g}"


def feature_distributions(
    session: Session, ctx: ScoringContext, org_id: uuid.UUID, sample: int
) -> DistributionsResponse:
    """Distributions over the most recent ``sample`` scored assessments of this organization.

    Bars are the share of the sample in each bin (they sum to 1 per feature).
    """
    rows = session.execute(
        select(Transaction.amount, Transaction.category, RiskAssessment.features)
        .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .where(RiskAssessment.organization_id == org_id, RiskAssessment.status == "scored")
        .order_by(desc(RiskAssessment.scored_at))
        .limit(sample)
    ).all()
    n = len(rows)
    out: list[Distribution] = []

    counts = {c: 0 for c in MERCHANT_CATEGORIES}
    for r in rows:
        counts[r.category] = counts.get(r.category, 0) + 1
    out.append(
        Distribution(
            feature="category",
            title="Merchant Category",
            kind="categorical",
            bins=list(counts),
            edges=None,
            shares=[c / n if n else 0.0 for c in counts.values()],
        )
    )
    for feature, (title, edges, integer) in _NUMERIC_BINS.items():
        if feature == "amount":
            values = np.array([float(r.amount) for r in rows], dtype=np.float64)
        else:
            raw = [(r.features or {}).get(feature) for r in rows]
            values = np.array([v for v in raw if v is not None], dtype=np.float64)
        hist, _ = np.histogram(values, bins=edges)
        total = int(hist.sum())
        out.append(
            Distribution(
                feature=feature,
                title=title,
                kind="numeric",
                bins=[_bin_label(lo, hi, integer) for lo, hi in pairwise(edges)],
                edges=edges,
                shares=[h / total if total else 0.0 for h in hist.tolist()],
            )
        )
    t = ctx.policy.thresholds
    return DistributionsResponse(
        sample_size=n,
        policy_version=ctx.policy.version,
        thresholds={
            "challenge_at": t.challenge_at,
            "review_at": t.review_at,
            "decline_at": t.decline_at,
        },
        distributions=out,
    )
