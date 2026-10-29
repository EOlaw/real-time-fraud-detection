"""Read-only monitoring results for the caller's organization.

Only ``scope = organization`` runs of the caller's own organization are visible. Global runs
aggregate every tenant and are operator data (CLI only); a global or foreign run id is ``404``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from fraud_api.core.errors import APIError
from fraud_api.db.models import ModelVersion, MonitoringMetric, MonitoringRun


class RunOut(BaseModel):
    id: uuid.UUID
    model: str
    window_start: datetime
    window_end: datetime
    labels_as_of: datetime | None
    maturity_days: int | None
    status: str
    recommendation: str
    summary: dict[str, Any]
    calc_version: str
    finished_at: datetime
    acknowledged: bool


class MetricOut(BaseModel):
    metric: str
    segment: str
    value: float | None
    ci_low: float | None
    ci_high: float | None
    sample_size: int
    reference_size: int | None
    labeled: bool
    status: str
    thresholds: dict[str, Any]
    details: dict[str, Any]


class RunDetail(RunOut):
    metrics: list[MetricOut]


def _out(run: MonitoringRun, model: str) -> dict[str, Any]:
    return {
        "id": run.id,
        "model": model,
        "window_start": run.window_start,
        "window_end": run.window_end,
        "labels_as_of": run.labels_as_of,
        "maturity_days": run.maturity_days,
        "status": run.status,
        "recommendation": run.recommendation,
        "summary": run.summary,
        "calc_version": run.calc_version,
        "finished_at": run.finished_at,
        "acknowledged": run.acknowledged_at is not None,
    }


def list_runs(session: Session, org_id: uuid.UUID, limit: int) -> list[RunOut]:
    rows = session.execute(
        select(MonitoringRun, ModelVersion.name, ModelVersion.version)
        .join(ModelVersion, ModelVersion.id == MonitoringRun.model_version_id)
        .where(MonitoringRun.organization_id == org_id)
        .order_by(MonitoringRun.window_end.desc(), MonitoringRun.finished_at.desc())
        .limit(limit)
    ).all()
    return [RunOut(**_out(r, f"{n}@{v}")) for r, n, v in rows]


def run_detail(session: Session, org_id: uuid.UUID, run_id: uuid.UUID) -> RunDetail:
    row = session.execute(
        select(MonitoringRun, ModelVersion.name, ModelVersion.version)
        .join(ModelVersion, ModelVersion.id == MonitoringRun.model_version_id)
        .where(MonitoringRun.id == run_id, MonitoringRun.organization_id == org_id)
    ).first()
    if row is None:
        raise APIError(404, "not_found", "Monitoring run not found")
    run, name, version = row
    metrics = session.scalars(
        select(MonitoringMetric)
        .where(MonitoringMetric.run_id == run.id)
        .order_by(MonitoringMetric.labeled, MonitoringMetric.metric, MonitoringMetric.segment)
    ).all()
    return RunDetail(
        **_out(run, f"{name}@{version}"),
        metrics=[
            MetricOut(
                metric=m.metric,
                segment=m.segment,
                value=m.value,
                ci_low=m.ci_low,
                ci_high=m.ci_high,
                sample_size=m.sample_size,
                reference_size=m.reference_size,
                labeled=m.labeled,
                status=m.status,
                thresholds=m.thresholds,
                details={k: v for k, v in m.details.items() if k != "caveat"},
            )
            for m in metrics
        ],
    )
