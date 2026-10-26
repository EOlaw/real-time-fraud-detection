"""Promotion gate for a candidate model + policy.

``check_promotion`` runs every gate and never changes serving state. ``apply_promotion`` (only
after all gates pass) repoints ``ml/artifacts/current`` and replaces the active policy file,
keeping a copy of the previous policy under ``ml/configs/policies/`` for rollback.

Gates:
  artifact      hash-verified, pickle-free load with feature-contract check
  policy        policy loads (sandbox, monotonic thresholds) and is bound to this model version
  threshold     policy ``review_at`` equals the validation threshold recorded in the artifact
  parity        online feature computation (the API path) reproduces offline features and the
                model score for a sample of real events
  decisions     the policy produces a decision for every parity sample
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fraud_api.db.importer import load_canonical
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.online import HistoryEvent, compute_online_features
from fraud_ml.features.spec import MAX_LOOKBACK_US, NUMERIC_FEATURES, encode
from fraud_ml.models.artifact import ArtifactError, LoadedModel, load_artifact


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


def _parity(model: LoadedModel, data: pd.DataFrame, sample: int, seed: int) -> tuple[Gate, Any]:
    data = data.sort_values(["event_time", "event_id"], kind="mergesort").reset_index(drop=True)
    offline = compute_offline_features(data)
    rng = np.random.default_rng(seed)
    rows = np.sort(rng.choice(len(data), min(sample, len(data)), replace=False))
    t_us = (data["event_time"].astype("datetime64[us, UTC]").astype("int64")).to_numpy()
    by_customer = {c: idx for c, idx in data.groupby("customer_id", sort=False).indices.items()}
    amounts = data["amount"].to_numpy(np.float64)
    categories = data["category"].to_numpy(object)
    offline_values = {n: offline[n].to_numpy(np.float64) for n in NUMERIC_FEATURES}
    max_feature_diff = 0.0
    online_rows, offline_rows = [], []
    for i in rows:
        e = data.iloc[i]
        same = by_customer[e["customer_id"]]
        t = t_us[i]
        hist_idx = same[(t_us[same] < t) & (t_us[same] >= t - MAX_LOOKBACK_US)]
        history = [
            HistoryEvent(int(t_us[j]), float(amounts[j]), str(categories[j])) for j in hist_idx
        ]
        on = compute_online_features(
            e["event_time"].to_pydatetime(),
            float(e["amount"]),
            str(e["category"]),
            str(e["channel"]),
            history,
        )
        for name in NUMERIC_FEATURES:
            a, b = float(offline_values[name][i]), float(on[name])
            if math.isnan(a) or math.isnan(b):
                if not (math.isnan(a) and math.isnan(b)):
                    return Gate("parity", False, f"missingness differs for {name}"), None
                continue
            max_feature_diff = max(max_feature_diff, abs(a - b) / max(1.0, abs(a)))
        online_rows.append(on)
        offline_rows.append(offline.iloc[i])

    def matrix(rows_: list[Any]) -> Any:
        return encode(
            {n: [float(r[n]) for r in rows_] for n in NUMERIC_FEATURES},
            {"category": [r["category"] for r in rows_], "channel": [r["channel"] for r in rows_]},
        )

    s_on = model.predict_proba(matrix(online_rows))
    s_off = model.predict_proba(matrix(offline_rows))
    max_score_diff = float(np.max(np.abs(s_on - s_off)))
    ok = max_feature_diff <= 1e-9 and max_score_diff <= 1e-6
    detail = (
        f"{len(rows)} events; max relative feature diff {max_feature_diff:.2e}; "
        f"max score diff {max_score_diff:.2e}"
    )
    return Gate("parity", ok, detail), s_on


def check_promotion(
    artifact_dir: Path, policy_path: Path, data_path: Path, sample: int = 500, seed: int = 0
) -> list[Gate]:
    gates: list[Gate] = []
    try:
        model = load_artifact(artifact_dir)
        gates.append(
            Gate("artifact", True, f"{model.name}@{model.version} sha {model.sha256[:12]}")
        )
    except (ArtifactError, OSError) as exc:
        return [Gate("artifact", False, str(exc))]
    try:
        policy = DecisionPolicy.load(policy_path)
        policy.check_model(model.name, model.version)
        gates.append(
            Gate("policy", True, f"{policy.version} bound to {model.name}@{model.version}")
        )
    except (ValueError, OSError) as exc:
        return [*gates, Gate("policy", False, str(exc))]
    recorded = float(model.manifest["decision_threshold"]["value"])
    gates.append(
        Gate(
            "threshold",
            abs(policy.thresholds.review_at - recorded) <= 1e-6,
            f"policy review_at {policy.thresholds.review_at} vs artifact {recorded:.6f}",
        )
    )
    data, _ = load_canonical(data_path)
    parity, scores = _parity(model, data, sample, seed)
    gates.append(parity)
    if scores is not None:
        decided = [policy.decide(float(s)).decision for s in scores]
        gates.append(
            Gate(
                "decisions",
                len(decided) == len(scores),
                f"{len(decided)} decisions: {pd.Series(decided).value_counts().to_dict()}",
            )
        )
    return gates


def apply_promotion(
    artifact_dir: Path,
    policy_path: Path,
    current_link: Path,
    active_policy: Path,
    archive_dir: Path,
) -> str:
    """Repoint serving to the candidate. Call only after ``check_promotion`` passed."""
    previous = DecisionPolicy.load(active_policy)
    archive_dir.mkdir(parents=True, exist_ok=True)
    backup = archive_dir / f"{previous.model.name}-{previous.model.version}.yaml"
    if not backup.exists():
        shutil.copyfile(active_policy, backup)
    if artifact_dir.resolve().parent != current_link.parent.resolve():
        raise ArtifactError("candidate artifact must live next to the 'current' link")
    tmp_link = current_link.with_name(".current.tmp")
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(artifact_dir.name)
    tmp_link.replace(current_link)
    shutil.copyfile(policy_path, active_policy)
    return (
        f"promoted {artifact_dir.name}; previous policy saved as {backup}. Rollback: "
        f"ln -sfn {previous.model.name}-{previous.model.version} {current_link} && "
        f"cp {backup} {active_policy}"
    )
