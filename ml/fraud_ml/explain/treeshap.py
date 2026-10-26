"""SHAP explanations for the XGBoost fraud model.

Uses XGBoost's built-in TreeSHAP (``Booster.predict(..., pred_contribs=True)``), the exact
path-dependent TreeSHAP algorithm of Lundberg et al. for tree ensembles. No sampling or
approximation is involved.

**Units: log-odds.** Contributions explain the model's *margin* (log-odds of fraud), not the
probability. For each row: ``base_value + sum(contributions) == margin`` and
``probability == sigmoid(margin)``. Contributions are additive in log-odds only; they cannot be
summed in probability space. ``reconstruction_error`` verifies the identity on every explained row.

One-hot columns (``category=*``, ``channel=*``) are summed into a single ``category`` / ``channel``
contribution. Summing SHAP values over a feature group is still exact and additive.

SHAP values describe how *this model* used the features. They are not causal effects and not
evidence that a transaction is fraudulent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pandas as pd
import xgboost as xgb

from fraud_ml.features.spec import CATEGORICAL_FEATURES, MODEL_COLUMNS, NUMERIC_FEATURES

UNITS: Final = "log-odds (model margin); probability = sigmoid(base_value + sum(contributions))"
GROUPS: Final[tuple[str, ...]] = NUMERIC_FEATURES + tuple(CATEGORICAL_FEATURES)


class ExplanationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Contributions:
    values: npt.NDArray[np.float64]  # (n, len(MODEL_COLUMNS)) log-odds
    base_value: npt.NDArray[np.float64]  # (n,)
    margin: npt.NDArray[np.float64]  # (n,) model log-odds output
    probability: npt.NDArray[np.float64]  # (n,)

    @property
    def reconstruction_error(self) -> float:
        recon = self.base_value + self.values.sum(axis=1)
        return float(np.max(np.abs(recon - self.margin))) if len(recon) else 0.0

    def grouped(self) -> npt.NDArray[np.float64]:
        """(n, len(GROUPS)) contributions with one-hot columns summed per original feature."""
        cols = {c: i for i, c in enumerate(MODEL_COLUMNS)}
        out = np.empty((self.values.shape[0], len(GROUPS)))
        for g, name in enumerate(GROUPS):
            if name in CATEGORICAL_FEATURES:
                idx = [cols[f"{name}={v}"] for v in CATEGORICAL_FEATURES[name]]
                out[:, g] = self.values[:, idx].sum(axis=1)
            else:
                out[:, g] = self.values[:, cols[name]]
        return out


def tree_shap(
    booster: xgb.Booster, x: npt.NDArray[np.float32], tolerance: float = 1e-3
) -> Contributions:
    """Exact TreeSHAP contributions. Raises if they do not reconstruct the model's margin."""
    dm = xgb.DMatrix(x, feature_names=list(MODEL_COLUMNS), missing=np.nan)
    raw = booster.predict(dm, pred_contribs=True).astype(np.float64)
    margin = booster.predict(dm, output_margin=True).astype(np.float64)
    probability = booster.predict(dm).astype(np.float64)
    result = Contributions(
        values=raw[:, :-1], base_value=raw[:, -1], margin=margin, probability=probability
    )
    if result.reconstruction_error > tolerance:
        raise ExplanationError(
            f"SHAP contributions do not reconstruct the margin (max error "
            f"{result.reconstruction_error:.2e} > {tolerance:g})"
        )
    if len(margin) and float(np.max(np.abs(1 / (1 + np.exp(-margin)) - probability))) > 1e-5:
        raise ExplanationError("probability != sigmoid(margin): model is not a logistic objective")
    return result


def global_importance(c: Contributions) -> list[dict[str, float | str]]:
    grouped = c.grouped()
    rows: list[dict[str, float | str]] = [
        {
            "feature": name,
            "mean_abs_contribution": float(np.mean(np.abs(grouped[:, g]))),
            "mean_contribution": float(np.mean(grouped[:, g])),
        }
        for g, name in enumerate(GROUPS)
    ]
    return sorted(rows, key=lambda r: -float(r["mean_abs_contribution"]))


def explain_rows(
    c: Contributions, features: pd.DataFrame, labels: list[str], top_k: int = 8
) -> list[dict[str, Any]]:
    """Per-transaction explanations. ``features`` must contain only model features (no ids, no
    customer tokens): the displayed values are exactly the model inputs."""
    allowed = set(NUMERIC_FEATURES) | set(CATEGORICAL_FEATURES)
    extra = set(features.columns) - allowed
    if extra:
        raise ExplanationError(f"refusing to display non-feature columns: {sorted(extra)}")
    grouped = c.grouped()
    out = []
    for i, label in enumerate(labels):
        order = np.argsort(-np.abs(grouped[i]))[:top_k]
        contributions = []
        for g in order:
            name = GROUPS[g]
            value = features.iloc[i][name]
            contributions.append(
                {
                    "feature": name,
                    "value": None if isinstance(value, float) and np.isnan(value) else value,
                    "contribution_log_odds": float(grouped[i, g]),
                }
            )
        rest = float(grouped[i].sum() - grouped[i, order].sum())
        out.append(
            {
                "example": label,
                "units": UNITS,
                "base_value_log_odds": float(c.base_value[i]),
                "margin_log_odds": float(c.margin[i]),
                "probability": float(c.probability[i]),
                "top_contributions": contributions,
                "other_features_log_odds": rest,
                "reconstruction_error": float(
                    abs(c.base_value[i] + c.values[i].sum() - c.margin[i])
                ),
            }
        )
    return out
