"""Promotion gates (Phase 9): the Phase 3 gates plus evidence from stored data and monitoring.

Every gate **fails closed**: an exception, missing input or insufficient sample is a failed gate
with a reason, never a pass. ``check_all`` only reads; promotion and rollback are separate,
explicit, audited commands (``fraud_api.monitoring.cli promote|rollback``), and nothing here
retrains, activates or repoints a model.

Gates:
  artifact, policy, threshold, parity, decisions   Phase 3 (fraud_api.promotion)
  feature_contract   candidate feature spec/columns equal the serving code and the incumbent
  reproducibility    manifest names the dataset version of the canonical data, a git commit and
                     a training config; the registry (if it knows the version) has the same hash
  reference          a reference profile exists for the candidate (needed to monitor it)
  min_samples        ≥ MIN_SCORED stored scored assessments in the evidence window and
                     ≥ MIN_LABELS mature eligible labels with ≥ MIN_POSITIVES fraud labels
  drift              candidate scores on the stored feature snapshots vs the candidate's own
                     reference: score PSI and max feature PSI below the alert threshold
  performance        on mature eligible labels: candidate average precision at least the
                     incumbent's minus AP_TOLERANCE, and candidate calibration error (ECE)
                     ≤ MAX_ECE (labeled subset; the selection-bias caveat applies)
  monitoring         no unacknowledged ``alert``/``failed`` monitoring run of the serving model
                     finished in the last LOOKBACK_DAYS days
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
from sklearn.metrics import average_precision_score
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fraud_api.db.models import ModelVersion, MonitoringReference, MonitoringRun
from fraud_api.insights.analytics import Window
from fraud_api.labels.metrics import BIAS_CAVEAT
from fraud_api.monitoring import drift
from fraud_api.monitoring.drift import CALC_VERSION, PSI_ALERT
from fraud_api.monitoring.reference import VOCABULARY
from fraud_api.promotion import Gate, check_promotion
from fraud_ml.data.ingest import MANIFEST_FILE
from fraud_ml.evaluation.metrics import calibration_table
from fraud_ml.features.spec import MODEL_COLUMNS, NUMERIC_FEATURES, encode
from fraud_ml.models.artifact import ArtifactError, LoadedModel, load_artifact

MIN_SCORED: Final = 1000
MIN_LABELS: Final = 200
MIN_POSITIVES: Final = 20
AP_TOLERANCE: Final = 0.02
MAX_ECE: Final = 0.02
LOOKBACK_DAYS: Final = 30


def _guard(name: str, fn: Any) -> Gate:
    try:
        result: Gate = fn()
        return result
    except Exception as exc:  # fail closed, with a reason that contains no data values
        return Gate(name, False, f"gate could not be evaluated: {type(exc).__name__}: {exc}"[:300])


def feature_contract(candidate: LoadedModel, incumbent: LoadedModel | None) -> Gate:
    cols = list(candidate.manifest.get("feature_columns", []))
    if cols != list(MODEL_COLUMNS):
        return Gate("feature_contract", False, "candidate feature columns differ from serving code")
    if incumbent is not None:
        if candidate.manifest.get("feature_spec_version") != incumbent.manifest.get(
            "feature_spec_version"
        ):
            return Gate("feature_contract", False, "feature spec version differs from incumbent")
        if list(incumbent.manifest.get("feature_columns", [])) != cols:
            return Gate("feature_contract", False, "feature columns differ from incumbent")
    return Gate(
        "feature_contract",
        True,
        f"{candidate.manifest.get('feature_spec_version')}: {len(cols)} columns match",
    )


def reproducibility(session: Session, candidate: LoadedModel, data_path: Path) -> Gate:
    import json

    dataset = json.loads((data_path.parent / MANIFEST_FILE).read_text())
    problems = []
    if dataset.get("dataset_version") != candidate.manifest.get("dataset_version"):
        problems.append("dataset version differs from the canonical data")
    if not candidate.manifest.get("git_commit"):
        problems.append("no git commit recorded")
    if not candidate.manifest.get("training_config"):
        problems.append("no training config recorded")
    mv = session.scalar(
        select(ModelVersion).where(
            ModelVersion.name == candidate.name, ModelVersion.version == candidate.version
        )
    )
    if mv is not None and mv.artifact_sha256 != candidate.sha256:
        problems.append("registry holds different bytes for this version")
    if problems:
        return Gate("reproducibility", False, "; ".join(problems))
    return Gate(
        "reproducibility",
        True,
        f"dataset {candidate.manifest['dataset_version']}, commit "
        f"{str(candidate.manifest['git_commit'])[:12]}, sha {candidate.sha256[:12]}",
    )


def _reference(session: Session, model: LoadedModel) -> MonitoringReference | None:
    return session.scalar(
        select(MonitoringReference)
        .join(ModelVersion, ModelVersion.id == MonitoringReference.model_version_id)
        .where(
            ModelVersion.name == model.name,
            ModelVersion.version == model.version,
            MonitoringReference.calc_version == CALC_VERSION,
            MonitoringReference.artifact_sha256 == model.sha256,
        )
    )


_EVIDENCE_SQL = """
WITH known AS (
    SELECT l.* FROM fraud_labels l WHERE l.labeled_at <= :as_of
), cur AS (
    SELECT k.* FROM known k
    WHERE NOT EXISTS (SELECT 1 FROM known s WHERE s.supersedes_id = k.id)
)
SELECT ra.features, ra.risk_score, cur.label, cur.confidence,
       (t.event_time <= :cutoff) AS mature
FROM risk_assessments ra
JOIN transactions t ON t.id = ra.transaction_id
LEFT JOIN cur ON cur.transaction_id = t.id
WHERE ra.status = 'scored' AND ra.scored_at >= :start AND ra.scored_at < :end
  AND t.source = ANY(:sources)
  AND (CAST(:mv AS uuid) IS NULL OR ra.model_version_id = CAST(:mv AS uuid))
"""


def _matrix(rows: list[Any]) -> Any:
    numeric = {
        n: [np.nan if (r.features or {}).get(n) is None else float(r.features[n]) for r in rows]
        for n in NUMERIC_FEATURES
    }
    cats = {
        c: np.asarray([(r.features or {}).get(c) for r in rows], dtype=object) for c in VOCABULARY
    }
    return encode(numeric, cats)


def evidence_gates(
    session: Session,
    candidate: LoadedModel,
    incumbent_mv: uuid.UUID | None,
    window: Window,
    as_of: datetime,
    maturity_days: int,
    include_probable: bool,
) -> list[Gate]:
    rows = session.execute(
        text(_EVIDENCE_SQL),
        {
            "as_of": as_of,
            "cutoff": as_of - timedelta(days=maturity_days),
            "start": window.start,
            "end": window.end,
            "sources": list(window.sources),
            "mv": incumbent_mv,
        },
    ).all()
    usable = [
        r
        for r in rows
        if r.mature
        and r.label in ("fraud", "legitimate")
        and (r.confidence == "confirmed" or include_probable)
    ]
    positives = sum(r.label == "fraud" for r in usable)
    enough = len(rows) >= MIN_SCORED and len(usable) >= MIN_LABELS and positives >= MIN_POSITIVES
    gates = [
        Gate(
            "min_samples",
            enough,
            f"{len(rows)} scored (min {MIN_SCORED}); {len(usable)} mature eligible labels "
            f"(min {MIN_LABELS}), {positives} fraud (min {MIN_POSITIVES})",
        )
    ]
    ref = _reference(session, candidate)
    if ref is None:
        gates.append(Gate("reference", False, "no reference profile for the candidate"))
        gates.append(Gate("drift", False, "cannot measure drift without a reference profile"))
    else:
        gates.append(Gate("reference", True, f"reference {ref.id} ({ref.sample_size} events)"))
        if len(rows) < drift.MIN_SAMPLE:
            gates.append(Gate("drift", False, f"insufficient data ({len(rows)} scored)"))
        else:
            scores = candidate.predict_proba(_matrix(list(rows))).astype(np.float64)
            score_psi = drift.numeric_psi(ref.profile["score"], scores).value or 0.0
            feature_psi = {
                n: drift.numeric_psi(
                    ref.profile["features"][n],
                    drift.as_float([(r.features or {}).get(n) for r in rows]),
                ).value
                or 0.0
                for n in NUMERIC_FEATURES
            }
            worst = max(feature_psi, key=lambda k: feature_psi[k])
            ok = score_psi < PSI_ALERT and feature_psi[worst] < PSI_ALERT
            gates.append(
                Gate(
                    "drift",
                    ok,
                    f"candidate score PSI {score_psi:.4f}; max feature PSI {feature_psi[worst]:.4f}"
                    f" ({worst}); limit {PSI_ALERT}",
                )
            )
    if not enough:
        gates.append(Gate("performance", False, "insufficient mature eligible labels"))
    else:
        y = np.asarray([1 if r.label == "fraud" else 0 for r in usable], dtype=np.int64)
        cand = candidate.predict_proba(_matrix(usable)).astype(np.float64)
        inc = np.asarray([float(r.risk_score) for r in usable])
        ap_c, ap_i = float(average_precision_score(y, cand)), float(average_precision_score(y, inc))
        ece = float(calibration_table(y, cand)["expected_calibration_error"])
        ok = ap_c >= ap_i - AP_TOLERANCE and ece <= MAX_ECE
        gates.append(
            Gate(
                "performance",
                ok,
                f"labeled subset n={len(usable)}: AP candidate {ap_c:.4f} vs incumbent {ap_i:.4f}"
                f" (tolerance {AP_TOLERANCE}); candidate ECE {ece:.4f} (max {MAX_ECE}). "
                f"{BIAS_CAVEAT}",
            )
        )
    return gates


def monitoring_failures(session: Session, serving_mv: uuid.UUID | None, now: datetime) -> Gate:
    if serving_mv is None:
        return Gate("monitoring", False, "no active model version to check")
    open_runs = session.scalars(
        select(MonitoringRun).where(
            MonitoringRun.model_version_id == serving_mv,
            MonitoringRun.status.in_(["alert", "failed"]),
            MonitoringRun.acknowledged_at.is_(None),
            MonitoringRun.finished_at >= now - timedelta(days=LOOKBACK_DAYS),
        )
    ).all()
    if open_runs:
        return Gate(
            "monitoring",
            False,
            f"{len(open_runs)} unacknowledged alert/failed monitoring run(s) in the last "
            f"{LOOKBACK_DAYS} days: {', '.join(str(r.id) for r in open_runs[:5])}",
        )
    return Gate("monitoring", True, f"no unacknowledged alert/failed runs in {LOOKBACK_DAYS} days")


def check_all(
    session: Session,
    artifact_dir: Path,
    policy_path: Path,
    data_path: Path,
    incumbent_dir: Path | None,
    window: Window,
    as_of: datetime | None = None,
    maturity_days: int = 30,
    include_probable: bool = False,
    parity_sample: int = 500,
) -> list[Gate]:
    """Run every gate (read-only). Any failed gate blocks promotion."""
    gates = check_promotion(artifact_dir, policy_path, data_path, parity_sample)
    if not gates or not gates[0].passed:
        return gates
    try:
        candidate = load_artifact(artifact_dir)
        incumbent = load_artifact(incumbent_dir) if incumbent_dir else None
    except (ArtifactError, OSError) as exc:
        return [*gates, Gate("feature_contract", False, str(exc))]
    now = datetime.now(UTC)
    serving = session.scalar(select(ModelVersion.id).where(ModelVersion.status == "active"))
    gates.append(_guard("feature_contract", lambda: feature_contract(candidate, incumbent)))
    gates.append(_guard("reproducibility", lambda: reproducibility(session, candidate, data_path)))
    try:
        gates.extend(
            evidence_gates(
                session, candidate, serving, window, as_of or now, maturity_days, include_probable
            )
        )
    except Exception as exc:  # fail closed
        session.rollback()
        gates.append(Gate("evidence", False, f"could not evaluate: {type(exc).__name__}"))
    gates.append(_guard("monitoring", lambda: monitoring_failures(session, serving, now)))
    return gates
