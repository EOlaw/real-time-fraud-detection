"""Feature specification shared by offline training and online scoring.

Point-in-time rule (applies to every behavioral feature): for an event at time ``t`` for customer
``c``, history is the set of *other* events of ``c`` with ``event_time < t`` (strict). A window of
length ``w`` covers ``[t - w, t)``. Events with the same timestamp never see each other, which
keeps features independent of arrival order within a timestamp.

Labels are never used as features, so delayed labels cannot leak into training rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

import numpy as np
import numpy.typing as npt

from fraud_ml.domains.card_payments import CHANNELS, MERCHANT_CATEGORIES

FEATURE_SPEC_VERSION: Final = "card-v1"

US_PER_SECOND: Final = 1_000_000
WINDOWS_US: Final[Mapping[str, int]] = {
    "1h": 3600 * US_PER_SECOND,
    "24h": 24 * 3600 * US_PER_SECOND,
    "7d": 7 * 24 * 3600 * US_PER_SECOND,
    "30d": 30 * 24 * 3600 * US_PER_SECOND,
}
MAX_LOOKBACK_US: Final = WINDOWS_US["30d"]

# Std floor (currency units) used in the z-score so that customers with near-constant spend do not
# produce unbounded z-scores.
ZSCORE_STD_FLOOR: Final = 1.0

NUMERIC_FEATURES: Final[tuple[str, ...]] = (
    "log_amount",
    "hour_of_day",
    "hour_sin",
    "hour_cos",
    "day_of_week",
    "is_night",
    "cust_txn_count_1h",
    "cust_txn_count_24h",
    "cust_txn_count_7d",
    "cust_txn_count_30d",
    "cust_amt_sum_24h",
    "cust_amt_mean_30d",
    "cust_amt_std_30d",
    "amt_zscore_30d",
    "amt_to_mean_ratio_30d",
    "seconds_since_prev_txn",
    "cust_same_category_count_30d",
)
CATEGORICAL_FEATURES: Final[Mapping[str, tuple[str, ...]]] = {
    "category": MERCHANT_CATEGORIES,
    "channel": CHANNELS,
}
MODEL_COLUMNS: Final[tuple[str, ...]] = NUMERIC_FEATURES + tuple(
    f"{name}={value}" for name, values in CATEGORICAL_FEATURES.items() for value in values
)


def time_features(
    hour: npt.NDArray[np.int64], dow: npt.NDArray[np.int64]
) -> dict[str, npt.NDArray[np.float64]]:
    h = hour.astype(np.float64)
    return {
        "hour_of_day": h,
        "hour_sin": np.sin(2 * np.pi * h / 24.0),
        "hour_cos": np.cos(2 * np.pi * h / 24.0),
        "day_of_week": dow.astype(np.float64),
        "is_night": ((hour >= 22) | (hour < 6)).astype(np.float64),
    }


def derived_amount_features(
    amount: npt.NDArray[np.float64],
    mean_30d: npt.NDArray[np.float64],
    std_30d: npt.NDArray[np.float64],
) -> dict[str, npt.NDArray[np.float64]]:
    with np.errstate(divide="ignore", invalid="ignore"):
        zscore = (amount - mean_30d) / np.maximum(std_30d, ZSCORE_STD_FLOOR)
        ratio = np.where(mean_30d > 0, amount / mean_30d, np.nan)
    return {
        "log_amount": np.log1p(amount),
        "amt_zscore_30d": np.where(np.isnan(std_30d), np.nan, zscore),
        "amt_to_mean_ratio_30d": ratio,
    }


def encode(
    numeric: Mapping[str, Sequence[float] | npt.NDArray[np.float64]],
    categorical: Mapping[str, Sequence[str] | npt.NDArray[np.object_]],
) -> npt.NDArray[np.float32]:
    """Build the model matrix in the fixed ``MODEL_COLUMNS`` order.

    Missing numeric values stay NaN (XGBoost routes them natively; sklearn baselines impute).
    Out-of-vocabulary categories are rejected rather than silently encoded as all-zeros.
    """
    n = len(next(iter(numeric.values())))
    out = np.empty((n, len(MODEL_COLUMNS)), dtype=np.float32)
    for i, name in enumerate(NUMERIC_FEATURES):
        out[:, i] = np.asarray(numeric[name], dtype=np.float64)
    col = len(NUMERIC_FEATURES)
    for name, vocab in CATEGORICAL_FEATURES.items():
        values = np.asarray(categorical[name], dtype=object)
        unknown = set(values.tolist()) - set(vocab)
        if unknown:
            raise ValueError(f"Unknown {name} values: {sorted(unknown)[:5]}")
        for value in vocab:
            out[:, col] = values == value
            col += 1
    return out
