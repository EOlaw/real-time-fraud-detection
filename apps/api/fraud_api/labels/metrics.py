"""Case, label and label-matured performance aggregates (tenant-scoped, computed from stored rows).

Two kinds of numbers are kept apart on purpose:

* **Labeled performance** uses only transactions whose outcome could have matured and whose
  label was known at ``as_of``. By default only ``confirmed`` fraud/legitimate labels count;
  ``probable`` labels are included only when requested, and ``inconclusive`` labels and unlabeled
  transactions are *never* counted as legitimate - they are reported as excluded.
* **Unlabeled signals** (alert rate, mean score over every assessment in the window) need no
  labels and are reported separately. Score and feature drift monitoring is Phase 9.

Labeled metrics describe the *labeled subset*, which is not a random sample: analysts review
alerted transactions, chargebacks arrive mostly for fraud, and dataset labels are synthetic. Recall
here is recall among labeled transactions, not the fraud capture rate of the whole population.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from fraud_api.insights.analytics import Window
from fraud_api.insights.analytics_schemas import Rate, WindowOut

BIAS_CAVEAT = (
    "Labeled metrics cover only transactions with a mature, usable label. That subset is selected "
    "(analysts review alerts; chargebacks skew toward fraud; dataset labels are synthetic), so it "
    "is not unbiased ground truth. Unlabeled transactions are excluded, never treated as "
    "legitimate."
)


def _rate(n: int, d: int) -> Rate:
    return Rate(numerator=n, denominator=d, rate=n / d if d else None)


# ---------------------------------------------------------------- cases


class CaseStats(BaseModel):
    window: WindowOut
    opened: int
    opened_by_severity: dict[str, int]
    resolved: int
    resolved_by_outcome: dict[str, int]
    hours_to_resolution_p50: float | None
    hours_to_resolution_p90: float | None
    open_now_by_status: dict[str, int] = Field(description="Snapshot at request time")
    open_now_unassigned: int


def case_stats(session: Session, org_id: uuid.UUID, w: Window) -> CaseStats:
    p = {"org": org_id, "start": w.start, "end": w.end, "sources": list(w.sources)}
    base = """
        FROM cases c JOIN transactions t ON t.id = c.transaction_id
        WHERE c.organization_id = :org AND t.source = ANY(:sources)
    """
    opened = session.execute(
        text(
            f"SELECT c.severity, count(*) {base} AND c.opened_at >= :start "
            "AND c.opened_at < :end GROUP BY 1"
        ),
        p,
    ).all()
    resolved = session.execute(
        text(
            f"SELECT c.status, count(*) {base} AND c.resolved_at >= :start "
            "AND c.resolved_at < :end GROUP BY 1"
        ),
        p,
    ).all()
    pct = session.execute(
        text(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY h), "  # noqa: S608
            "percentile_cont(0.9) WITHIN GROUP (ORDER BY h) FROM (SELECT "
            f"extract(epoch FROM c.resolved_at - c.opened_at) / 3600.0 AS h {base} "
            "AND c.resolved_at >= :start AND c.resolved_at < :end) x"
        ),
        p,
    ).one()
    open_now = session.execute(
        text(
            f"SELECT c.status, count(*), count(*) FILTER (WHERE c.assigned_to IS NULL) {base} "
            "AND c.resolved_at IS NULL GROUP BY 1"
        ),
        p,
    ).all()
    sev = {s: 0 for s in ("low", "medium", "high", "critical")} | {k: int(n) for k, n in opened}
    out = {s: 0 for s in ("resolved_fraud", "resolved_legitimate", "inconclusive")} | {
        k: int(n) for k, n in resolved
    }
    status = {s: 0 for s in ("new", "under_review", "escalated")} | {
        k: int(n) for k, n, _ in open_now
    }
    return CaseStats(
        window=w.out(),
        opened=sum(sev.values()),
        opened_by_severity=sev,
        resolved=sum(out.values()),
        resolved_by_outcome=out,
        hours_to_resolution_p50=float(pct[0]) if pct[0] is not None else None,
        hours_to_resolution_p90=float(pct[1]) if pct[1] is not None else None,
        open_now_by_status=status,
        open_now_unassigned=sum(int(u) for _, _, u in open_now),
    )


# ---------------------------------------------------------------- labels


class LabelCountRow(BaseModel):
    label: str
    source: str
    confidence: str
    count: int


class LabelStats(BaseModel):
    window: WindowOut
    recorded: int = Field(
        description="Label rows with labeled_at in the window (incl. corrections)"
    )
    corrections: int = Field(description="Of those, rows that supersede an earlier label")
    current: list[LabelCountRow] = Field(
        description="Current (not superseded) labels with labeled_at in the window"
    )
    note: str


def label_stats(session: Session, org_id: uuid.UUID, w: Window) -> LabelStats:
    p = {"org": org_id, "start": w.start, "end": w.end, "sources": list(w.sources)}
    base = """
        FROM fraud_labels l JOIN transactions t ON t.id = l.transaction_id
        WHERE l.organization_id = :org AND t.source = ANY(:sources)
          AND l.labeled_at >= :start AND l.labeled_at < :end
    """
    rec = session.execute(
        text(f"SELECT count(*), count(*) FILTER (WHERE l.supersedes_id IS NOT NULL) {base}"), p
    ).one()
    rows = session.execute(
        text(
            f"SELECT l.label, l.source, l.confidence, count(*) {base} AND NOT EXISTS "  # noqa: S608
            "(SELECT 1 FROM fraud_labels s WHERE s.supersedes_id = l.id) "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ),
        p,
    ).all()
    return LabelStats(
        window=w.out(),
        recorded=int(rec[0]),
        corrections=int(rec[1]),
        current=[
            LabelCountRow(label=a, source=b, confidence=c, count=int(n)) for a, b, c, n in rows
        ],
        note="Dataset labels are the synthetic dataset's is_fraud column. Analyst, chargeback and "
        "customer-report labels are provenance-tracked observations, not automatic ground truth.",
    )


# ---------------------------------------------------------------- label-matured performance


class Confusion(BaseModel):
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: Rate = Field(description="TP / (TP + FP) among labeled alerts")
    recall: Rate = Field(description="TP / (TP + FN) among labeled fraud (not population recall)")
    false_positive_rate: Rate = Field(description="FP / (FP + TN) among labeled legitimate")


class Population(BaseModel):
    assessments_in_window: int
    immature: int = Field(description="event_time later than the maturity cutoff; excluded")
    mature: int
    mature_unlabeled: int = Field(description="Excluded; never treated as legitimate")
    mature_inconclusive: int = Field(description="Excluded")
    mature_probable_excluded: int = Field(description="Excluded unless include_probable=true")
    usable: int = Field(description="Mature, current label fraud/legitimate, confidence allowed")
    usable_fraud: int
    usable_legitimate: int
    label_coverage: Rate = Field(description="usable / mature")
    usable_by_source: dict[str, int]


class UnlabeledSignals(BaseModel):
    assessments: int
    alert_rate: Rate = Field(description="decision != allow, all assessments in the window")
    mean_risk_score: float | None = Field(description="Scored assessments only")
    note: str


class Performance(BaseModel):
    window: WindowOut = Field(description="Cohort selected by transaction event_time")
    as_of: datetime = Field(description="Only labels with labeled_at <= as_of are used")
    maturity_days: int
    maturity_cutoff: datetime = Field(description="Events after this are immature")
    include_probable: bool
    population: Population
    final_decision: Confusion = Field(description="Alert = final decision != allow")
    policy_decision: Confusion = Field(description="Alert = policy decision != allow")
    mean_score_fraud: float | None
    mean_score_legitimate: float | None
    unlabeled_signals: UnlabeledSignals
    caveat: str


_PERF_SQL = """
WITH known AS (
    SELECT l.* FROM fraud_labels l
    WHERE l.organization_id = :org AND l.labeled_at <= :as_of
), cur AS (
    -- the label known at as_of: not superseded by another label already known at as_of
    SELECT k.* FROM known k
    WHERE NOT EXISTS (SELECT 1 FROM known s WHERE s.supersedes_id = k.id)
), cohort AS (
    SELECT ra.decision, ra.policy_decision, ra.risk_score, t.event_time,
           cur.label, cur.confidence, cur.source AS label_source,
           (t.event_time <= :cutoff) AS mature,
           (cur.label IN ('fraud', 'legitimate')
            AND (cur.confidence = 'confirmed' OR :include_probable)) AS usable,
           (t.event_time <= :cutoff AND cur.label IN ('fraud', 'legitimate')
            AND (cur.confidence = 'confirmed' OR :include_probable)) AS mu
    FROM risk_assessments ra
    JOIN transactions t ON t.id = ra.transaction_id
    LEFT JOIN cur ON cur.transaction_id = t.id
    WHERE ra.organization_id = :org AND t.organization_id = :org
      AND t.event_time >= :start AND t.event_time < :end
      AND t.source = ANY(:sources)
      AND (CAST(:mv AS uuid) IS NULL OR ra.model_version_id = CAST(:mv AS uuid))
)
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE NOT mature) AS immature,
    count(*) FILTER (WHERE mature) AS mature,
    count(*) FILTER (WHERE mature AND label IS NULL) AS unlabeled,
    count(*) FILTER (WHERE mature AND label = 'inconclusive') AS inconclusive,
    count(*) FILTER (WHERE mature AND label IN ('fraud','legitimate')
                     AND confidence = 'probable' AND NOT :include_probable) AS probable_excluded,
    count(*) FILTER (WHERE mu) AS usable,
    count(*) FILTER (WHERE mu AND label = 'fraud') AS usable_fraud,
    count(*) FILTER (WHERE mu AND label = 'legitimate') AS usable_legit,
    count(*) FILTER (WHERE mu AND label = 'fraud' AND decision <> 'allow') AS f_tp,
    count(*) FILTER (WHERE mu AND label = 'legitimate' AND decision <> 'allow') AS f_fp,
    count(*) FILTER (WHERE mu AND label = 'fraud' AND decision = 'allow') AS f_fn,
    count(*) FILTER (WHERE mu AND label = 'legitimate' AND decision = 'allow') AS f_tn,
    count(*) FILTER (WHERE mu AND label = 'fraud' AND policy_decision <> 'allow')
        AS p_tp,
    count(*) FILTER (WHERE mu AND label = 'legitimate' AND policy_decision <> 'allow')
        AS p_fp,
    count(*) FILTER (WHERE mu AND label = 'fraud' AND policy_decision = 'allow')
        AS p_fn,
    count(*) FILTER (WHERE mu AND label = 'legitimate' AND policy_decision = 'allow')
        AS p_tn,
    avg(risk_score) FILTER (WHERE mu AND label = 'fraud') AS score_fraud,
    avg(risk_score) FILTER (WHERE mu AND label = 'legitimate') AS score_legit,
    count(*) FILTER (WHERE decision <> 'allow') AS all_alerts,
    avg(risk_score) AS all_score
FROM cohort
"""

_SOURCES_SQL = (
    _PERF_SQL.split("SELECT\n    count(*) AS total")[0]
    + """
SELECT label_source, count(*) FROM cohort WHERE mu GROUP BY 1
"""
)


def _confusion(tp: int, fp: int, fn: int, tn: int) -> Confusion:
    return Confusion(
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        true_negative=tn,
        precision=_rate(tp, tp + fp),
        recall=_rate(tp, tp + fn),
        false_positive_rate=_rate(fp, fp + tn),
    )


def performance(
    session: Session,
    org_id: uuid.UUID,
    w: Window,
    as_of: datetime,
    maturity_days: int,
    include_probable: bool,
    model_version_id: uuid.UUID | None = None,
) -> Performance:
    cutoff = as_of - timedelta(days=maturity_days)
    params: dict[str, Any] = {
        "org": org_id,
        "start": w.start,
        "end": w.end,
        "sources": list(w.sources),
        "as_of": as_of,
        "cutoff": cutoff,
        "include_probable": include_probable,
        "mv": model_version_id,
    }
    r = session.execute(text(_PERF_SQL), params).one()._mapping
    by_source = {s: int(n) for s, n in session.execute(text(_SOURCES_SQL), params).all()}
    n = {k: int(v) if isinstance(v, int) else v for k, v in r.items()}
    return Performance(
        window=w.out(),
        as_of=as_of,
        maturity_days=maturity_days,
        maturity_cutoff=cutoff,
        include_probable=include_probable,
        population=Population(
            assessments_in_window=n["total"],
            immature=n["immature"],
            mature=n["mature"],
            mature_unlabeled=n["unlabeled"],
            mature_inconclusive=n["inconclusive"],
            mature_probable_excluded=n["probable_excluded"],
            usable=n["usable"],
            usable_fraud=n["usable_fraud"],
            usable_legitimate=n["usable_legit"],
            label_coverage=_rate(n["usable"], n["mature"]),
            usable_by_source=by_source,
        ),
        final_decision=_confusion(n["f_tp"], n["f_fp"], n["f_fn"], n["f_tn"]),
        policy_decision=_confusion(n["p_tp"], n["p_fp"], n["p_fn"], n["p_tn"]),
        mean_score_fraud=float(n["score_fraud"]) if n["score_fraud"] is not None else None,
        mean_score_legitimate=float(n["score_legit"]) if n["score_legit"] is not None else None,
        unlabeled_signals=UnlabeledSignals(
            assessments=n["total"],
            alert_rate=_rate(n["all_alerts"], n["total"]),
            mean_risk_score=float(n["all_score"]) if n["all_score"] is not None else None,
            note="Computed over every assessment in the window, labeled or not. Score and "
            "feature drift monitoring is not built yet (Phase 9).",
        ),
        caveat=BIAS_CAVEAT,
    )
