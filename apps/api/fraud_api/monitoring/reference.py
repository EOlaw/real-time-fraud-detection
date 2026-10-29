"""Reference profiles: the distributions monitoring compares against.

The reference for a model version is that model's **own validation window** (from its manifest)
of its **own dataset version**: the data its decision thresholds were chosen on. Building one:

1. checks that the canonical dataset's version equals the artifact's ``dataset_version`` and that
   the artifact hash matches the registry (a profile can never describe another model or data);
2. computes offline point-in-time features for events up to the end of the window (earlier events
   are history only) and scores the window with the artifact;
3. records, per feature: reference deciles, bin proportions (incl. a missing bin), a 101-point
   quantile grid and the missing rate; categorical proportions over the closed vocabulary plus
   ``__other__``/``__missing__``; score bins and quantiles; and the model-bound policy's decision
   mix and alert rate.

The profile holds aggregates only (no event rows, ids, customers or labels). Profiles are
append-only; rebuilding an identical profile is a no-op, a different one for the same model
version and calculation version is refused.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from fraud_api.db.models import AuditLog, ModelVersion, MonitoringReference
from fraud_api.monitoring.drift import (
    CALC_VERSION,
    categorical_profile,
    numeric_profile,
)
from fraud_api.registry.service import register_model
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.domains.card_payments import CHANNELS, MERCHANT_CATEGORIES
from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import NUMERIC_FEATURES
from fraud_ml.models.artifact import LoadedModel
from fraud_ml.training.train import build_matrix

VOCABULARY = {"category": MERCHANT_CATEGORIES, "channel": CHANNELS}
DECISIONS = ("allow", "challenge", "review", "decline")


class MonitoringError(RuntimeError):
    """A monitoring precondition failed; the message is safe to show and log."""


def policy_decisions(policy: DecisionPolicy, scores: np.ndarray) -> np.ndarray:
    t = policy.thresholds
    out = np.full(len(scores), "allow", dtype=object)
    out[scores >= t.challenge_at] = "challenge"
    out[scores >= t.review_at] = "review"
    out[scores >= t.decline_at] = "decline"
    return out


def validation_window(manifest: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp, bool]:
    """(start, end, end_inclusive) of the model's validation split, from its manifest.

    Training runs record the observed ``time_range`` (inclusive); experiment runs record the
    configured boundaries ``[train_end, validation_end)``.
    """
    split = manifest.get("splits", {}).get("validation", {})
    if "time_range" in split:
        a, b = split["time_range"]
        return pd.Timestamp(a), pd.Timestamp(b), True
    cfg = manifest.get("training_config", {})
    bounds = cfg.get("experiment", {}).get("splits") or cfg.get("splits")
    if bounds and "train_end" in bounds and "validation_end" in bounds:
        return pd.Timestamp(bounds["train_end"]), pd.Timestamp(bounds["validation_end"]), False
    raise MonitoringError("the manifest does not record the validation window")


def build_profile(
    model: LoadedModel, policy: DecisionPolicy, events: pd.DataFrame
) -> tuple[dict[str, Any], datetime, datetime]:
    start, end, inclusive = validation_window(model.manifest)
    upto = events["event_time"] <= end if inclusive else events["event_time"] < end
    history = events[upto].sort_values(["event_time", "event_id"], kind="mergesort")
    features = compute_offline_features(history)
    in_window = (history["event_time"] >= start).to_numpy()
    feats = features[in_window]
    if len(feats) == 0:
        raise MonitoringError("the reference window has no events in this dataset")
    expected = model.manifest.get("splits", {}).get("validation", {}).get("rows")
    if expected is not None and int(expected) != len(feats):
        raise MonitoringError(
            f"the reference window holds {len(feats)} events but the model's validation split "
            f"had {expected}: wrong dataset or window"
        )
    scores = model.predict_proba(build_matrix(feats)).astype(np.float64)
    decisions = policy_decisions(policy, scores)
    profile: dict[str, Any] = {
        "features": {n: numeric_profile(feats[n].to_numpy(np.float64)) for n in NUMERIC_FEATURES},
        "categorical": {
            c: categorical_profile(feats[c].tolist(), vocab) for c, vocab in VOCABULARY.items()
        },
        "score": numeric_profile(scores),
        "decisions": {d: float((decisions == d).mean()) for d in DECISIONS},
        "alert_rate": float((decisions != "allow").mean()),
        "n": len(feats),
        "policy_version": policy.version,
        "thresholds": policy.thresholds.model_dump(),
    }
    return profile, start.to_pydatetime(), end.to_pydatetime()


def profile_sha256(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def store_reference(
    session: Session,
    model: LoadedModel,
    policy: DecisionPolicy,
    dataset_manifest: dict[str, Any],
    profile: dict[str, Any],
    window: tuple[datetime, datetime],
) -> tuple[MonitoringReference, bool]:
    """Persist (or return the identical existing) reference. Returns (reference, created)."""
    if dataset_manifest.get("dataset_version") != model.manifest.get("dataset_version"):
        raise MonitoringError(
            f"dataset version {dataset_manifest.get('dataset_version')} is not the model's "
            f"training dataset {model.manifest.get('dataset_version')}"
        )
    policy.check_model(model.name, model.version)
    existing_mv = session.scalar(
        select(ModelVersion).where(
            ModelVersion.name == model.name, ModelVersion.version == model.version
        )
    )
    if existing_mv is not None and existing_mv.artifact_sha256 != model.sha256:
        raise MonitoringError("artifact hash differs from the registered version")
    # Registers (never activates) the version if it is unknown; refuses different bytes.
    mv_id: uuid.UUID = register_model(session, model, policy, activate=False)
    digest = profile_sha256(profile)
    current = session.scalar(
        select(MonitoringReference).where(
            MonitoringReference.model_version_id == mv_id,
            MonitoringReference.calc_version == CALC_VERSION,
        )
    )
    if current is not None:
        if current.profile_sha256 != digest:
            raise MonitoringError(
                "a different reference profile already exists for this model version and "
                "calculation version; references are immutable"
            )
        return current, False
    ref = MonitoringReference(
        model_version_id=mv_id,
        artifact_sha256=model.sha256,
        dataset_version=str(model.manifest["dataset_version"]),
        window_start=window[0],
        window_end=window[1],
        sample_size=int(profile["n"]),
        profile=profile,
        profile_sha256=digest,
        calc_version=CALC_VERSION,
    )
    session.add(ref)
    session.flush()
    session.add(
        AuditLog(
            action="monitoring.reference",
            resource_type="monitoring_reference",
            resource_id=str(ref.id),
            details={
                "model": f"{model.name}@{model.version}",
                "dataset_version": ref.dataset_version,
                "window": [window[0].isoformat(), window[1].isoformat()],
                "sample_size": ref.sample_size,
                "calc_version": CALC_VERSION,
            },
        )
    )
    session.commit()
    return ref, True
