"""Tenant-scoped aggregates over persisted transactions, assessments and rule evaluations.

Every number is computed by PostgreSQL from stored rows at request time; nothing is cached or
hardcoded. Windows are half-open ``[start, end)`` in UTC and bounded (``MAX_WINDOW``). Rates carry
their numerator and denominator, and a rate over an empty denominator is ``null``, not 0.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from fraud_api.core.errors import APIError
from fraud_api.db.models import RiskAssessment, RuleDefinition, RuleEvaluation, Transaction
from fraud_api.insights.analytics_schemas import (
    AssessmentCounts,
    Basis,
    Bucket,
    Distribution,
    LatencyOut,
    OverviewOut,
    Rate,
    RuleHitRow,
    RuleHitsOut,
    Source,
    TransactionCounts,
    VolumeBucket,
    VolumeOut,
    WindowOut,
)

MAX_WINDOW: Final = timedelta(days=366)
MAX_BUCKETS: Final = 1000
SOURCES: Final[tuple[str, ...]] = get_args(Source)
DECISIONS: Final = ("allow", "challenge", "review", "decline")
RISK_LEVELS: Final = ("low", "medium", "high", "critical", "unknown")
STATUSES: Final = ("scored", "degraded")
RULES_STATUSES: Final = ("none", "evaluated", "error")
_BUCKET_SIZE: Final = {"hour": timedelta(hours=1), "day": timedelta(days=1)}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    basis: Basis
    sources: tuple[str, ...]

    def out(self) -> WindowOut:
        return WindowOut(
            start=self.start,
            end=self.end,
            basis=self.basis,
            sources=list(self.sources),
        )


def make_window(
    start: datetime, end: datetime, basis: Basis, sources: list[Source] | None
) -> Window:
    if start.tzinfo is None or end.tzinfo is None:
        raise APIError(400, "invalid_window", "start and end must include a timezone offset")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end <= start:
        raise APIError(400, "invalid_window", "end must be after start")
    if end - start > MAX_WINDOW:
        raise APIError(400, "invalid_window", f"window is longer than {MAX_WINDOW.days} days")
    chosen = tuple(s for s in SOURCES if s in set(sources)) if sources else SOURCES
    return Window(start, end, basis, chosen)


def _rate(numerator: int, denominator: int) -> Rate:
    return Rate(
        numerator=numerator,
        denominator=denominator,
        rate=numerator / denominator if denominator else None,
    )


def _assessment_time(w: Window) -> Any:
    return RiskAssessment.scored_at if w.basis == "processed" else Transaction.event_time


def _transaction_time(w: Window) -> Any:
    return Transaction.received_at if w.basis == "processed" else Transaction.event_time


def _assessments(stmt: Select[Any], org_id: uuid.UUID, w: Window) -> Select[Any]:
    col = _assessment_time(w)
    return stmt.where(
        RiskAssessment.organization_id == org_id,
        Transaction.organization_id == org_id,
        col >= w.start,
        col < w.end,
        Transaction.source.in_(w.sources),
    )


def _base_assessments(*columns: Any) -> Select[Any]:
    return (
        select(*columns)
        .select_from(RiskAssessment)
        .join(Transaction, Transaction.id == RiskAssessment.transaction_id)
    )


def _counts(column: Any, values: tuple[str, ...], prefix: str) -> list[ColumnElement[Any]]:
    return [func.count().filter(column == v).label(f"{prefix}:{v}") for v in values]


def overview(session: Session, org_id: uuid.UUID, w: Window) -> OverviewOut:
    tcol = _transaction_time(w)
    txn = session.execute(
        select(
            func.count().label("received"),
            func.count(RiskAssessment.id).label("scored"),
        )
        .select_from(Transaction)
        .outerjoin(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .where(
            Transaction.organization_id == org_id,
            tcol >= w.start,
            tcol < w.end,
            Transaction.source.in_(w.sources),
        )
    ).one()

    dims = {
        "by_status": (RiskAssessment.status, STATUSES),
        "by_decision": (RiskAssessment.decision, DECISIONS),
        "by_policy_decision": (RiskAssessment.policy_decision, DECISIONS),
        "by_risk_level": (RiskAssessment.risk_level, RISK_LEVELS),
        "by_source": (Transaction.source, SOURCES),
        "by_rules_status": (RiskAssessment.rules_status, RULES_STATUSES),
    }
    columns: list[ColumnElement[Any]] = [func.count().label("total")]
    for name, (col, values) in dims.items():
        columns += _counts(col, values, name)
    columns.append(
        func.count().filter(RiskAssessment.decision != RiskAssessment.policy_decision).label("esc")
    )
    row = session.execute(_assessments(_base_assessments(*columns), org_id, w)).one()._mapping
    total = int(row["total"])
    breakdown = {
        name: {v: int(row[f"{name}:{v}"]) for v in values} for name, (_, values) in dims.items()
    }
    return OverviewOut(
        window=w.out(),
        transactions=TransactionCounts(
            received=txn.received, scored=txn.scored, unscored=txn.received - txn.scored
        ),
        assessments=AssessmentCounts(total=total, **breakdown),
        alert_rate=_rate(total - breakdown["by_decision"]["allow"], total),
        policy_alert_rate=_rate(total - breakdown["by_policy_decision"]["allow"], total),
        rule_escalation_rate=_rate(int(row["esc"]), total),
        degraded_rate=_rate(breakdown["by_status"]["degraded"], total),
    )


def volume(session: Session, org_id: uuid.UUID, w: Window, bucket: Bucket) -> VolumeOut:
    size = _BUCKET_SIZE[bucket]
    for t in (w.start, w.end):
        aligned = t.replace(minute=0, second=0, microsecond=0)
        if bucket == "day":
            aligned = aligned.replace(hour=0)
        if t != aligned:
            raise APIError(
                400, "invalid_window", f"start and end must be aligned to UTC {bucket} boundaries"
            )
    n = int((w.end - w.start) / size)
    if n > MAX_BUCKETS:
        raise APIError(400, "invalid_window", f"window spans more than {MAX_BUCKETS} buckets")

    tcol = _transaction_time(w)
    tb = func.date_trunc(bucket, tcol, "UTC").label("b")
    txn_counts = {
        r.b: r.n
        for r in session.execute(
            select(tb, func.count().label("n"))
            .where(
                Transaction.organization_id == org_id,
                tcol >= w.start,
                tcol < w.end,
                Transaction.source.in_(w.sources),
            )
            .group_by(tb)
        ).all()
    }
    ab = func.date_trunc(bucket, _assessment_time(w), "UTC").label("b")
    stmt = _base_assessments(
        ab,
        func.count().label("n"),
        *_counts(RiskAssessment.decision, DECISIONS, "d"),
    )
    assess = {r.b: r._mapping for r in session.execute(_assessments(stmt, org_id, w).group_by(ab))}
    buckets = []
    for i in range(n):
        start = w.start + i * size
        a = assess.get(start)
        by_decision = {d: int(a[f"d:{d}"]) if a else 0 for d in DECISIONS}
        total = int(a["n"]) if a else 0
        buckets.append(
            VolumeBucket(
                start=start,
                transactions=int(txn_counts.get(start, 0)),
                assessments=total,
                alerts=total - by_decision["allow"],
                by_decision=by_decision,
            )
        )
    return VolumeOut(window=w.out(), bucket=bucket, buckets=buckets)


def rule_hits(session: Session, org_id: uuid.UUID, w: Window) -> RuleHitsOut:
    totals = session.execute(
        _assessments(
            _base_assessments(
                func.count().label("n"),
                func.count().filter(RiskAssessment.rules_status != "none").label("with_rules"),
                func.count()
                .filter(RiskAssessment.decision != RiskAssessment.policy_decision)
                .label("escalated"),
            ),
            org_id,
            w,
        )
    ).one()
    ev = RuleEvaluation
    in_window = _assessments(
        select(RiskAssessment.id)
        .select_from(RiskAssessment)
        .join(Transaction, Transaction.id == RiskAssessment.transaction_id),
        org_id,
        w,
    ).subquery()
    with_match = session.scalar(
        select(func.count(func.distinct(ev.risk_assessment_id))).where(
            ev.organization_id == org_id,
            ev.outcome == "matched",
            ev.risk_assessment_id.in_(select(in_window.c.id)),
        )
    )
    rows = session.execute(
        select(
            RuleDefinition.id,
            RuleDefinition.rule_key,
            RuleDefinition.version,
            RuleDefinition.action,
            func.count().label("evaluated"),
            *[
                func.count().filter(ev.outcome == o).label(o)
                for o in ("matched", "unknown", "error")
            ],
            func.count().filter(ev.influenced_decision).label("influenced"),
        )
        .select_from(ev)
        .join(RuleDefinition, RuleDefinition.id == ev.rule_definition_id)
        .where(
            ev.organization_id == org_id,
            RuleDefinition.organization_id == org_id,
            ev.risk_assessment_id.in_(select(in_window.c.id)),
        )
        .group_by(
            RuleDefinition.id,
            RuleDefinition.rule_key,
            RuleDefinition.version,
            RuleDefinition.action,
        )
    ).all()
    out = [
        RuleHitRow(
            rule_id=r.id,
            rule_key=r.rule_key,
            version=r.version,
            action=r.action,
            evaluated=r.evaluated,
            matched=r.matched,
            unknown=r.unknown,
            errors=r.error,
            influenced=r.influenced,
            match_rate=r.matched / r.evaluated if r.evaluated else None,
        )
        for r in rows
    ]
    out.sort(key=lambda h: (-h.matched, h.rule_key, -h.version))
    return RuleHitsOut(
        window=w.out(),
        assessments=totals.n,
        assessments_with_rules=totals.with_rules,
        assessments_with_match=with_match or 0,
        assessments_escalated=totals.escalated,
        rules=out,
    )


_PERCENTILES: Final = {"p50": 0.5, "p90": 0.9, "p95": 0.95, "p99": 0.99}


def _distribution_columns(value: Any, prefix: str) -> list[ColumnElement[Any]]:
    return [
        func.count(value).label(f"{prefix}:count"),
        func.avg(value).label(f"{prefix}:mean"),
        func.max(value).label(f"{prefix}:max"),
        *[
            func.percentile_cont(q).within_group(value).label(f"{prefix}:{name}")
            for name, q in _PERCENTILES.items()
        ],
    ]


def _distribution(row: Any, prefix: str) -> Distribution:
    def f(key: str) -> float | None:
        v = row[f"{prefix}:{key}"]
        return float(v) if v is not None else None

    return Distribution(
        count=int(row[f"{prefix}:count"]),
        mean=f("mean"),
        max=f("max"),
        **{name: f(name) for name in _PERCENTILES},
    )


def latency(session: Session, org_id: uuid.UUID, w: Window) -> LatencyOut:
    components = ("features", "inference", "rules")

    def timing(key: str) -> Any:
        # NULL when the key is absent (e.g. no features/inference on a degraded score, or no
        # rules timing on rows written before Phase 6); aggregates skip NULLs.
        return RiskAssessment.timings_ms[key].as_float()

    columns = _distribution_columns(timing("total"), "total")
    for c in components:
        columns += _distribution_columns(timing(c), c)
    row = session.execute(_assessments(_base_assessments(*columns), org_id, w)).one()._mapping
    return LatencyOut(
        window=w.out(),
        processing_ms=_distribution(row, "total"),
        components_ms={c: _distribution(row, c) for c in components},
    )
