"""Unsupervised behavioral anomaly score (Isolation Forest), kept separate from fraud probability.

What it measures: how *unusual* a transaction's behavioral profile is relative to the bulk of
historical traffic (amount relative to the customer's own baseline, velocity, recency, time of day).
It is fit **without labels** and is **not** a probability: scores are unbounded, uncalibrated, and
only meaningful as a ranking ("higher = more isolated").

It must never be fed into the decision policy or the fraud model, or blended with ``risk_score``.
Three mechanisms enforce this:
  * ``AnomalyScores`` is a distinct type with no numeric conversion; ``DecisionPolicy.decide``
    rejects anything that is not a plain probability float.
  * Anomaly feature names are disjoint from ``MODEL_COLUMNS`` and never added to model artifacts.
  * ``fit`` takes no labels and refuses frames that contain a label column.

Rare-but-legitimate behaviour (a large one-off purchase) is anomalous; fraud that mimics normal
behaviour is not. The evaluation therefore reports whether the score adds signal, rather than
assuming it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.ensemble import IsolationForest

DETECTOR_KIND: Final = "isolation_forest"
ANOMALY_FEATURES: Final[tuple[str, ...]] = (
    "log_amount",
    "hour_sin",
    "hour_cos",
    "cust_txn_count_1h",
    "cust_txn_count_24h",
    "cust_amt_sum_24h",
    "amt_zscore_30d",
    "amt_to_mean_ratio_30d",
    "seconds_since_prev_txn",
    "cust_same_category_count_30d",
)
# Heavy-tailed inputs are log-transformed so isolation is not dominated by raw magnitude.
_LOG1P: Final = frozenset({"cust_amt_sum_24h", "seconds_since_prev_txn", "amt_to_mean_ratio_30d"})
# Features that can be missing (no history); their missingness is added as indicator columns.
_NULLABLE: Final = ("amt_zscore_30d", "amt_to_mean_ratio_30d", "seconds_since_prev_txn")
_FORBIDDEN_INPUTS: Final = frozenset({"is_fraud", "label", "risk_score", "fraud_probability"})


@dataclass(frozen=True)
class AnomalyScores:
    """Anomaly ranking scores. Deliberately not array-like and not a probability."""

    values: npt.NDArray[np.float64]
    detector: str
    units: str = (
        "isolation-forest anomaly score (higher = more anomalous; unbounded; not a probability)"
    )


class AnomalyInputError(ValueError):
    pass


class BehavioralAnomalyDetector:
    def __init__(self, n_estimators: int = 200, max_samples: int = 256, seed: int = 0) -> None:
        self._forest = IsolationForest(
            n_estimators=n_estimators, max_samples=max_samples, random_state=seed, n_jobs=-1
        )
        self._medians: npt.NDArray[np.float64] | None = None

    @staticmethod
    def _check(features: pd.DataFrame) -> None:
        bad = _FORBIDDEN_INPUTS & set(features.columns)
        if bad:
            raise AnomalyInputError(f"anomaly detector must not see {sorted(bad)}")

    def _matrix(self, features: pd.DataFrame) -> npt.NDArray[np.float64]:
        self._check(features)
        cols = []
        for name in ANOMALY_FEATURES:
            v = features[name].to_numpy(np.float64)
            cols.append(np.sign(v) * np.log1p(np.abs(v)) if name in _LOG1P else v)
        x = np.column_stack(cols)
        missing = np.isnan(x)
        if self._medians is None:
            med = np.nanmedian(x, axis=0)
            self._medians = np.where(np.isnan(med), 0.0, med)
        x = np.where(missing, self._medians, x)
        nullable = [ANOMALY_FEATURES.index(n) for n in _NULLABLE]
        return np.column_stack([x, missing[:, nullable].astype(np.float64)])

    def fit(self, features: pd.DataFrame) -> BehavioralAnomalyDetector:
        """Unsupervised fit: there is no ``y`` parameter by design."""
        self._check(features)  # validate before discarding any previous fit
        self._medians = None
        self._forest.fit(self._matrix(features))
        return self

    def score(self, features: pd.DataFrame) -> AnomalyScores:
        if self._medians is None:
            raise RuntimeError("detector is not fitted")
        # sklearn's score_samples is higher for normal points; negate so higher = more anomalous.
        return AnomalyScores(
            values=-self._forest.score_samples(self._matrix(features)), detector=DETECTOR_KIND
        )
