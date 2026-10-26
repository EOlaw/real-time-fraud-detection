"""Class-imbalance strategies, applied to training rows only.

Callers pass *only* the training portion (a CV fold's train rows, or the training window). Nothing
here sees validation or test rows, so synthetic or dropped rows can never leak into evaluation.

Strategies:
  ``none``          baseline: data unchanged.
  ``class_weight``  XGBoost ``scale_pos_weight = negatives / positives``.
  ``undersample``   random majority-class undersampling to ``minority/majority = ratio``.
  ``smotenc``       SMOTE-NC oversampling to ``minority/majority = ratio``.

SMOTE on the 34-column model matrix would interpolate one-hot columns (producing e.g.
"0.4 grocery_pos + 0.6 travel"), interpolate NaNs (which XGBoost treats as "no history"), and break
deterministic relationships such as ``channel = f(category)`` or ``hour_sin = sin(hour)``. Instead
SMOTE-NC runs on the *primitive* features: category, hour and weekday are categorical (copied from
neighbours, never interpolated), and missingness is encoded as categorical indicators.
Continuous features are standardized with statistics from the training rows only. Every derived
feature is then recomputed from the primitives with the same functions the feature pipeline uses,
so each synthetic row satisfies the feature definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
import numpy.typing as npt
import pandas as pd
from imblearn.over_sampling import SMOTENC
from imblearn.under_sampling import RandomUnderSampler

from fraud_ml.domains.card_payments import MERCHANT_CATEGORIES, derive_channel
from fraud_ml.features.spec import NUMERIC_FEATURES, derived_amount_features, time_features

Strategy = Literal["none", "class_weight", "undersample", "smotenc"]
STRATEGIES: Final[tuple[Strategy, ...]] = ("none", "class_weight", "undersample", "smotenc")

_CONTINUOUS: Final = (
    "log_amount",
    "cust_amt_sum_24h",
    "cust_amt_mean_30d",
    "cust_amt_std_30d",
    "seconds_since_prev_txn",
)
_COUNTS: Final = (
    "cust_txn_count_1h",
    "cust_txn_count_24h",
    "cust_txn_count_7d",
    "cust_txn_count_30d",
    "cust_same_category_count_30d",
)
# Missingness carried as a categorical indicator. The 30-day mean/std are not listed: by
# definition they are null exactly when the 30-day count is < 1 / < 2, so their missingness is
# re-derived from the (rounded) synthetic counts.
_NULLABLE: Final = ("seconds_since_prev_txn",)


@dataclass(frozen=True)
class ResampleConfig:
    undersample_ratio: float = 0.1
    smote_ratio: float = 0.1
    smote_k_neighbors: int = 5


@dataclass
class Resampled:
    features: pd.DataFrame  # NUMERIC_FEATURES + category + channel
    y: npt.NDArray[np.int64]
    xgb_params: dict[str, Any] = field(default_factory=dict)
    synthetic_rows: int = 0
    dropped_rows: int = 0


def _rebuild_derived(prim: pd.DataFrame) -> pd.DataFrame:
    """Recompute every derived feature from primitives (same functions as the feature pipeline)."""
    hour = prim["hour_of_day"].to_numpy(np.int64)
    dow = prim["day_of_week"].to_numpy(np.int64)
    amount = np.expm1(prim["log_amount"].to_numpy(np.float64))
    mean = prim["cust_amt_mean_30d"].to_numpy(np.float64)
    std = prim["cust_amt_std_30d"].to_numpy(np.float64)
    out = {
        **time_features(hour, dow),
        **derived_amount_features(amount, mean, std),
        **{c: prim[c].to_numpy(np.float64) for c in _COUNTS + _CONTINUOUS if c != "log_amount"},
    }
    frame = pd.DataFrame(out, index=prim.index)[list(NUMERIC_FEATURES)]
    frame["category"] = prim["category"].to_numpy()
    frame["channel"] = [derive_channel(c) for c in prim["category"]]
    return frame


def _smotenc(
    features: pd.DataFrame, y: npt.NDArray[np.int64], cfg: ResampleConfig, seed: int
) -> tuple[pd.DataFrame, npt.NDArray[np.int64]]:
    n_pos = int(y.sum())
    if n_pos <= cfg.smote_k_neighbors:
        raise ValueError(f"SMOTE-NC needs more than {cfg.smote_k_neighbors} positives, got {n_pos}")

    cont = features[list(_CONTINUOUS)].to_numpy(np.float64)
    missing = np.isnan(cont)
    medians = np.nanmedian(cont, axis=0)
    medians = np.where(np.isnan(medians), 0.0, medians)
    cont = np.where(missing, medians, cont)
    mu, sd = cont.mean(axis=0), cont.std(axis=0)
    sd = np.where(sd > 0, sd, 1.0)
    cont = (cont - mu) / sd  # fold-train statistics only

    counts = features[list(_COUNTS)].to_numpy(np.float64)
    cmu, csd = counts.mean(axis=0), counts.std(axis=0)
    csd = np.where(csd > 0, csd, 1.0)
    counts = (counts - cmu) / csd

    cat_code = pd.Categorical(features["category"], categories=MERCHANT_CATEGORIES).codes
    indicators = [missing[:, _CONTINUOUS.index(c)].astype(np.float64) for c in _NULLABLE]
    categorical = np.column_stack(
        [
            cat_code.astype(np.float64),
            features["hour_of_day"].to_numpy(np.float64),
            features["day_of_week"].to_numpy(np.float64),
            *indicators,
        ]
    )
    x = np.column_stack([cont, counts, categorical])
    n_num = cont.shape[1] + counts.shape[1]
    sampler = SMOTENC(
        categorical_features=list(range(n_num, x.shape[1])),
        sampling_strategy=cfg.smote_ratio,
        k_neighbors=cfg.smote_k_neighbors,
        random_state=seed,
    )
    xr, yr = sampler.fit_resample(x, y)

    c = xr[:, : len(_CONTINUOUS)] * sd + mu
    k = np.rint(xr[:, len(_CONTINUOUS) : n_num] * csd + cmu).clip(min=0)
    cats = xr[:, n_num:]
    prim = pd.DataFrame(c, columns=list(_CONTINUOUS))
    for i, name in enumerate(_COUNTS):
        prim[name] = k[:, i]
    prim["category"] = np.asarray(MERCHANT_CATEGORIES, dtype=object)[cats[:, 0].astype(int)]
    prim["hour_of_day"] = cats[:, 1].astype(int)
    prim["day_of_week"] = cats[:, 2].astype(int)
    # Restore missingness, then enforce the structural rules of the feature definitions.
    for j, name in enumerate(_NULLABLE):
        prim.loc[cats[:, 3 + j] == 1, name] = np.nan
    n30 = prim["cust_txn_count_30d"]
    prim.loc[n30 < 1, "cust_amt_mean_30d"] = np.nan
    prim.loc[n30 < 2, "cust_amt_std_30d"] = np.nan
    prim.loc[prim["cust_txn_count_24h"] < 1, "cust_amt_sum_24h"] = 0.0
    prim["cust_amt_sum_24h"] = prim["cust_amt_sum_24h"].clip(lower=0)
    prim["cust_amt_std_30d"] = prim["cust_amt_std_30d"].clip(lower=0)
    prim["seconds_since_prev_txn"] = prim["seconds_since_prev_txn"].clip(lower=0)
    rebuilt = _rebuild_derived(prim)
    # Original rows are returned unchanged (exact values, not a lossy round trip).
    n = len(features)
    original = features[[*list(NUMERIC_FEATURES), "category", "channel"]].reset_index(drop=True)
    combined = pd.concat([original, rebuilt.iloc[n:].reset_index(drop=True)], ignore_index=True)
    return combined, np.asarray(yr, dtype=np.int64)


def resample(
    strategy: Strategy,
    features: pd.DataFrame,
    y: npt.NDArray[np.int64],
    cfg: ResampleConfig,
    seed: int,
) -> Resampled:
    """Apply ``strategy`` to training rows. Returns features (not yet one-hot encoded)."""
    cols = [*list(NUMERIC_FEATURES), "category", "channel"]
    base = features[cols].reset_index(drop=True)
    if strategy == "none":
        return Resampled(base, y.copy())
    if strategy == "class_weight":
        pos = max(int(y.sum()), 1)
        return Resampled(base, y.copy(), {"scale_pos_weight": float((y == 0).sum() / pos)})
    if strategy == "undersample":
        rus = RandomUnderSampler(sampling_strategy=cfg.undersample_ratio, random_state=seed)
        idx, yr = rus.fit_resample(np.arange(len(y)).reshape(-1, 1), y)
        keep = np.sort(idx.ravel())
        return Resampled(
            base.iloc[keep].reset_index(drop=True),
            y[keep].astype(np.int64),
            dropped_rows=len(y) - len(keep),
        )
    if strategy == "smotenc":
        out, yr = _smotenc(base, y, cfg, seed)
        return Resampled(out, yr, synthetic_rows=len(yr) - len(y))
    raise ValueError(f"unknown strategy {strategy!r}")
