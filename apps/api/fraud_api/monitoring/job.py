"""Monitoring runs over stored assessments and labels.

Inputs are stored rows only: ``risk_assessments`` (score, decisions, status and the feature
snapshot taken at scoring time), ``transactions.source`` and ``event_time``, and ``fraud_labels``.
No raw payloads, customer or merchant identifiers are read, and nothing is written outside the
monitoring tables and ``audit_logs`` (regression-tested).

A run is identified by (scope, model version, window, calculation version). Running it again
updates the same run and metric rows in place: idempotent. Bootstrap seeds derive from that
identity, so identical data gives identical numbers.

Signals are kept apart:

* **Unlabeled** (``labeled = false``): score PSI with a bootstrap interval and KS, per-feature
  PSI/KS over the stored snapshot, categorical PSI, policy-decision PSI and alert-rate change
  against the reference, plus operational rates (degraded, rule escalation, excluded versions).
  Per-source segments where the segment has enough samples.
* **Labeled** (``labeled = true``): label-matured performance from
  ``labels.metrics.performance``. Labels must be mature, known at ``labels_as_of`` and eligible by
  confidence; missing, immature, inconclusive and (by default) probable labels are excluded and
  counted, never treated as legitimate. The selection-bias caveat is stored with each value.

The run status is the worst metric status. ``alert`` recommends a retraining review, ``warn`` a
review. Nothing here retrains, promotes or changes the serving model.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from fraud_api.core.logging import get_logger
from fraud_api.db.models import (
    NIL_UUID,
    AuditLog,
    ModelVersion,
    MonitoringMetric,
    MonitoringReference,
    MonitoringRun,
    Organization,
    RiskAssessment,
    Transaction,
)
from fraud_api.insights.analytics import Window
from fraud_api.labels.metrics import BIAS_CAVEAT, performance
from fraud_api.monitoring import drift
from fraud_api.monitoring.drift import CALC_VERSION, MIN_SAMPLE, Result
from fraud_api.monitoring.reference import VOCABULARY, MonitoringError
from fraud_api.observability.metrics import DB_ERRORS
from fraud_ml.features.spec import NUMERIC_FEATURES

log = get_logger(__name__)

MIN_LABELS: Final = 200
MIN_POSITIVES: Final = 20
SOURCES: Final = ("live", "replay", "simulated")


@dataclass
class Sample:
    scores: list[float] = field(default_factory=list)
    features: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    policy: list[str] = field(default_factory=list)
    final: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    degraded: int = 0
    other_versions: int = 0
    escalated: int = 0


def serving_model_version(session: Session) -> uuid.UUID:
    mv = session.scalar(select(ModelVersion.id).where(ModelVersion.status == "active"))
    if mv is None:
        raise MonitoringError("no active model version in the registry")
    return mv


def _reference(session: Session, model_version_id: uuid.UUID) -> MonitoringReference:
    ref = session.scalar(
        select(MonitoringReference).where(
            MonitoringReference.model_version_id == model_version_id,
            MonitoringReference.calc_version == CALC_VERSION,
        )
    )
    if ref is None:
        raise MonitoringError(
            "no reference profile for this model version; build one with "
            "`python -m fraud_api.monitoring.cli build-reference`"
        )
    mv = session.get(ModelVersion, model_version_id)
    if mv is None or mv.artifact_sha256 != ref.artifact_sha256:
        raise MonitoringError("reference_version_mismatch: reference was built for other bytes")
    return ref


def _load(
    session: Session, org_id: uuid.UUID | None, model_version_id: uuid.UUID, w: Window
) -> Sample:
    stmt = (
        select(
            RiskAssessment.model_version_id,
            RiskAssessment.status,
            RiskAssessment.risk_score,
            RiskAssessment.policy_decision,
            RiskAssessment.decision,
            RiskAssessment.features,
            Transaction.source,
        )
        .join(Transaction, Transaction.id == RiskAssessment.transaction_id)
        .where(RiskAssessment.scored_at >= w.start, RiskAssessment.scored_at < w.end)
        .where(Transaction.source.in_(w.sources))
        .execution_options(yield_per=5000)
    )
    if org_id is not None:
        stmt = stmt.where(RiskAssessment.organization_id == org_id)
    s = Sample()
    for mv, status, score, policy_d, final_d, feats, source in session.execute(stmt):
        if status != "scored":
            s.degraded += 1
            continue
        if mv != model_version_id:
            s.other_versions += 1  # scored by another model version: not this reference
            continue
        s.scores.append(float(score))
        s.policy.append(policy_d)
        s.final.append(final_d)
        s.sources.append(source)
        s.escalated += int(final_d != policy_d)
        f = feats or {}
        for name in NUMERIC_FEATURES:
            s.features[name].append(f.get(name))
        for name in VOCABULARY:
            s.features[name].append(f.get(name))
    return s


def _seed(*parts: Any) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def _distribution(
    profile: dict[str, Any], s: Sample, seed: int
) -> dict[tuple[str, str], tuple[Result, bool]]:
    out: dict[tuple[str, str], tuple[Result, bool]] = {}
    scores = np.asarray(s.scores, dtype=np.float64)
    score_psi = drift.numeric_psi(profile["score"], scores)
    if score_psi.value is not None:
        ci = drift.bootstrap_psi_ci(profile["score"], scores, seed)
        score_psi = Result(**{**score_psi.__dict__, "ci": ci})
    out[("score_psi", "all")] = (score_psi, False)
    out[("score_ks", "all")] = (drift.ks(profile["score"], scores), False)
    for name in NUMERIC_FEATURES:
        values = drift.as_float(s.features.get(name, []))
        out[(f"feature_psi:{name}", "all")] = (
            drift.numeric_psi(profile["features"][name], values),
            False,
        )
        out[(f"feature_ks:{name}", "all")] = (drift.ks(profile["features"][name], values), False)
    for name, vocab in VOCABULARY.items():
        out[(f"category_psi:{name}", "all")] = (
            drift.categorical_psi(profile["categorical"][name], s.features.get(name, []), vocab),
            False,
        )
    decisions = ("allow", "challenge", "review", "decline")
    n = len(s.policy)
    if n >= MIN_SAMPLE:
        counts = [sum(1 for d in s.policy if d == k) for k in decisions]
        value = drift.psi([profile["decisions"][k] for k in decisions], counts)
        out[("policy_decision_psi", "all")] = (
            Result(
                value,
                drift.psi_status(value),
                n,
                profile["n"],
                thresholds={"warn": drift.PSI_WARN, "alert": drift.PSI_ALERT},
            ),
            False,
        )
    else:
        out[("policy_decision_psi", "all")] = (
            Result(None, "insufficient_data", n, profile["n"]),
            False,
        )
    alerts = sum(1 for d in s.policy if d != "allow")
    out[("policy_alert_rate", "all")] = (
        drift.rate_change(alerts, n, profile["alert_rate"], profile["n"]),
        False,
    )
    # per-source segments (unlabeled only)
    for src in SOURCES:
        idx = [i for i, x in enumerate(s.sources) if x == src]
        if not idx:
            continue
        seg = f"source:{src}"
        out[("score_psi", seg)] = (drift.numeric_psi(profile["score"], scores[idx]), False)
        seg_alerts = sum(1 for i in idx if s.policy[i] != "allow")
        out[("policy_alert_rate", seg)] = (
            drift.rate_change(seg_alerts, len(idx), profile["alert_rate"], profile["n"]),
            False,
        )
    # operational rates: informational (no thresholds)
    total = n + s.degraded
    for metric, k, d in (
        ("degraded_rate", s.degraded, total),
        ("rule_escalation_rate", s.escalated, n),
        ("final_alert_rate", sum(1 for d in s.final if d != "allow"), n),
    ):
        out[(metric, "all")] = (
            Result(
                k / d if d else None,
                "ok" if d else "insufficient_data",
                d,
                None,
                ci=drift.wilson(k, d),
                details={"numerator": k, "denominator": d},
            ),
            False,
        )
    out[("excluded_other_model_versions", "all")] = (
        Result(
            float(s.other_versions),
            "ok",
            s.other_versions,
            None,
            details={"note": "scored by a different model version; not compared"},
        ),
        False,
    )
    return out


def _labeled(
    session: Session,
    orgs: list[uuid.UUID],
    model_version_id: uuid.UUID,
    cohort: Window,
    as_of: datetime,
    maturity_days: int,
    include_probable: bool,
) -> dict[tuple[str, str], tuple[Result, bool]]:
    keys = (
        "usable",
        "usable_fraud",
        "mature",
        "immature",
        "mature_unlabeled",
        "mature_inconclusive",
        "mature_probable_excluded",
    )
    pop = dict.fromkeys(keys, 0)
    conf = {f"{p}_{k}": 0 for p in ("final", "policy") for k in ("tp", "fp", "fn", "tn")}
    for org in orgs:
        p = performance(
            session,
            org,
            cohort,
            as_of,
            maturity_days,
            include_probable,
            model_version_id=model_version_id,
        )
        for k in keys:
            pop[k] += getattr(p.population, k)
        for prefix, c in (("final", p.final_decision), ("policy", p.policy_decision)):
            conf[f"{prefix}_tp"] += c.true_positive
            conf[f"{prefix}_fp"] += c.false_positive
            conf[f"{prefix}_fn"] += c.false_negative
            conf[f"{prefix}_tn"] += c.true_negative
    enough = pop["usable"] >= MIN_LABELS and pop["usable_fraud"] >= MIN_POSITIVES
    details = {
        **pop,
        **conf,
        "caveat": BIAS_CAVEAT,
        "as_of": as_of.isoformat(),
        "maturity_days": maturity_days,
        "include_probable": include_probable,
        "min_labels": MIN_LABELS,
        "min_positives": MIN_POSITIVES,
    }
    out: dict[tuple[str, str], tuple[Result, bool]] = {}
    for prefix in ("final", "policy"):
        tp, fp, fn, tn = (conf[f"{prefix}_{k}"] for k in ("tp", "fp", "fn", "tn"))
        for metric, num, den in (
            (f"precision_{prefix}", tp, tp + fp),
            (f"recall_{prefix}", tp, tp + fn),
            (f"false_positive_rate_{prefix}", fp, fp + tn),
        ):
            value = num / den if den else None
            status: drift.Status = "ok" if enough and den else "insufficient_data"
            out[(metric, "all")] = (
                Result(
                    value if status == "ok" else None,
                    status,
                    pop["usable"],
                    None,
                    ci=drift.wilson(num, den) if status == "ok" else None,
                    thresholds={"min_labels": MIN_LABELS, "min_positives": MIN_POSITIVES},
                    details={**details, "numerator": num, "denominator": den},
                ),
                True,
            )
    out[("label_coverage", "all")] = (
        Result(
            pop["usable"] / pop["mature"] if pop["mature"] else None,
            "ok" if pop["mature"] else "insufficient_data",
            pop["mature"],
            None,
            details=details,
        ),
        True,
    )
    return out


def run_monitoring(
    session: Session,
    *,
    organization_id: uuid.UUID | None,
    model_version_id: uuid.UUID | None,
    window: Window,
    labels_as_of: datetime,
    maturity_days: int,
    label_cohort: Window | None = None,
    include_probable: bool = False,
) -> MonitoringRun:
    """Compute and persist one run (idempotent). Raises MonitoringError for bad preconditions."""
    started = datetime.now(UTC)
    mv_id = model_version_id or serving_model_version(session)
    ref = _reference(session, mv_id)
    scope_key = organization_id or NIL_UUID
    seed = _seed(scope_key, mv_id, window.start, window.end, CALC_VERSION)
    try:
        sample = _load(session, organization_id, mv_id, window)
        metrics = _distribution(ref.profile, sample, seed)
        orgs = (
            [organization_id]
            if organization_id
            else list(session.scalars(select(Organization.id).order_by(Organization.id)))
        )
        metrics |= _labeled(
            session,
            orgs,
            mv_id,
            label_cohort or window,
            labels_as_of,
            maturity_days,
            include_probable,
        )
        status = drift.worst(
            [
                r.status
                for (_, seg), (r, labeled) in metrics.items()
                if seg == "all" and not labeled and r.thresholds
            ]
        )
        failure: str | None = None
    except SQLAlchemyError as exc:
        session.rollback()
        DB_ERRORS.labels("monitoring").inc()
        raise MonitoringError(f"database error: {type(exc).__name__}") from exc
    except (ValueError, KeyError) as exc:
        metrics, status, failure = {}, "failed", type(exc).__name__
        sample = Sample()
    recommendation = {"alert": "retraining_review", "warn": "review", "failed": "review"}.get(
        status, "none"
    )
    counts: dict[str, int] = defaultdict(int)
    for r, _ in metrics.values():
        counts[r.status] += 1
    alert_list = sorted(f"{m}[{g}]" for (m, g), (r, _) in metrics.items() if r.status == "alert")
    warning_list = sorted(f"{m}[{g}]" for (m, g), (r, _) in metrics.items() if r.status == "warn")
    summary = {
        "assessments": len(sample.scores),
        "degraded": sample.degraded,
        "excluded_other_model_versions": sample.other_versions,
        "metric_status_counts": dict(counts),
        "alerts": alert_list,
        "warnings": warning_list,
        "failure": failure,
        "reference": {
            "id": str(ref.id),
            "window": [ref.window_start.isoformat(), ref.window_end.isoformat()],
            "sample_size": ref.sample_size,
            "dataset_version": ref.dataset_version,
        },
        "caveat": BIAS_CAVEAT,
    }
    finished = datetime.now(UTC)
    values = {
        "scope": "organization" if organization_id else "global",
        "organization_id": organization_id,
        "scope_key": scope_key,
        "model_version_id": mv_id,
        "reference_id": ref.id,
        "window_start": window.start,
        "window_end": window.end,
        "labels_as_of": labels_as_of,
        "maturity_days": maturity_days,
        "status": status,
        "recommendation": recommendation,
        "summary": summary,
        "calc_version": CALC_VERSION,
        "started_at": started,
        "finished_at": finished,
    }
    try:
        stmt = insert(MonitoringRun).values(id=uuid.uuid4(), **values)
        run_id = session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_monitoring_runs_scope_window",
                set_={
                    **{
                        k: stmt.excluded[k]
                        for k in (
                            "status",
                            "recommendation",
                            "summary",
                            "labels_as_of",
                            "maturity_days",
                            "reference_id",
                            "started_at",
                            "finished_at",
                        )
                    },
                    # An acknowledgement covers the result it saw: a changed status clears it.
                    **{
                        k: case(
                            (
                                MonitoringRun.status == stmt.excluded.status,
                                getattr(MonitoringRun, k),
                            ),
                            else_=None,
                        )
                        for k in ("acknowledged_by", "acknowledged_at", "ack_note")
                    },
                },
            ).returning(MonitoringRun.id)
        ).scalar_one()
        for (metric, segment), (r, labeled) in metrics.items():
            row = {
                "run_id": run_id,
                "organization_id": organization_id,
                "model_version_id": mv_id,
                "metric": metric,
                "segment": segment,
                "window_start": window.start,
                "window_end": window.end,
                "value": r.value,
                "ci_low": r.ci[0] if r.ci else None,
                "ci_high": r.ci[1] if r.ci else None,
                "sample_size": r.sample_size,
                "reference_size": r.reference_size,
                "labeled": labeled,
                "status": r.status,
                "thresholds": r.thresholds,
                "details": r.details,
                "calc_version": CALC_VERSION,
                "computed_at": finished,
            }
            m = insert(MonitoringMetric).values(**row)
            session.execute(
                m.on_conflict_do_update(
                    constraint="uq_monitoring_metrics_run_metric",
                    set_={
                        k: m.excluded[k] for k in row if k not in ("run_id", "metric", "segment")
                    },
                )
            )
        session.add(
            AuditLog(
                organization_id=organization_id,
                action="monitoring.run",
                resource_type="monitoring_run",
                resource_id=str(run_id),
                details={
                    "status": status,
                    "recommendation": recommendation,
                    "metrics": len(metrics),
                    "assessments": len(sample.scores),
                },
            )
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        DB_ERRORS.labels("monitoring").inc()
        raise MonitoringError(f"database error: {type(exc).__name__}") from exc
    log.info(
        "monitoring_run",
        scope=values["scope"],
        status=status,
        recommendation=recommendation,
        assessments=len(sample.scores),
        degraded=sample.degraded,
        alerts=len(alert_list),
        warnings=len(warning_list),
    )
    run = session.get(MonitoringRun, run_id)
    assert run is not None
    session.refresh(run)
    return run


def acknowledge(
    session: Session, run_id: uuid.UUID, user_id: uuid.UUID, note: str
) -> MonitoringRun:
    run = session.get(MonitoringRun, run_id)
    if run is None:
        raise MonitoringError("run not found")
    if run.status not in ("warn", "alert", "failed"):
        raise MonitoringError("only warn, alert or failed runs need acknowledgement")
    run.acknowledged_by = user_id
    run.acknowledged_at = datetime.now(UTC)
    run.ack_note = note[:1000]
    session.add(
        AuditLog(
            organization_id=run.organization_id,
            actor_user_id=user_id,
            action="monitoring.acknowledge",
            resource_type="monitoring_run",
            resource_id=str(run.id),
            details={"status": run.status},
        )
    )
    session.commit()
    return run
