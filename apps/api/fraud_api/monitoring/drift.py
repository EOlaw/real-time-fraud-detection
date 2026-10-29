"""Drift statistics (pure numpy; documented in docs/ml/monitoring.md).

Definitions (calculation version ``CALC_VERSION``):

* **PSI** (population stability index) over the *reference* bins:
  ``PSI = Σ (c_i - r_i) · ln(c_i / r_i)`` where ``r_i``/``c_i`` are reference/current proportions.
  Empty bins are floored at ``EPS = 1e-4`` before the log (and proportions renormalised), so PSI
  stays finite; the floor is part of the definition and versioned with it.
* **Numeric bins**: reference deciles of the *non-missing* values (duplicate edges removed, so a
  feature with a point mass gets fewer bins) plus one **missing** bin (NaN/None). Missing values
  are therefore compared as their own category, never imputed.
* **Categorical bins**: the model's closed vocabulary (feature spec) plus ``__other__`` (values
  outside the vocabulary) and ``__missing__``.
* **KS**: max |F_cur(q) - F_ref(q)| over the reference's 101-point quantile grid ``q``
  (non-missing values), where ``F_ref(q)`` is the reference's *exact* empirical CDF at each grid
  value (stored in the profile). Exact CDF values matter for discrete features: with ties (binary
  flags, counts with a point mass at 0) the CDF at a quantile is not ``k/100``. Significance
  uses the two-sample critical value
  ``c(alpha) · sqrt((n + m) / (n · m))`` with alpha = 0.01 (c = 1.628).
* **Rates** (alert, decision): difference of proportions with a 95 % normal-approximation
  interval; Wilson intervals for single proportions.
* **Bootstrap**: score PSI gets a percentile 95 % interval from ``BOOTSTRAP`` resamples of the
  current window with a fixed seed (the reference is treated as fixed), so reruns are identical.

Thresholds are conventional, not tuned on this data: PSI < 0.10 stable, 0.10-0.25 ``warn``,
≥ 0.25 ``alert``; KS ``warn`` ≥ 0.05 / ``alert`` ≥ 0.10 and only when significant; alert-rate ratio
``warn`` outside [0.67, 1.5], ``alert`` outside [0.5, 2.0] and only when the interval of the
difference excludes 0. Fewer than ``MIN_SAMPLE`` current values → ``insufficient_data`` (never
``ok``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import numpy as np
import numpy.typing as npt

CALC_VERSION: Final = "drift-v1"
EPS: Final = 1e-4
MIN_SAMPLE: Final = 500
BOOTSTRAP: Final = 200
KS_C_ALPHA_01: Final = 1.628
PSI_WARN: Final = 0.10
PSI_ALERT: Final = 0.25
KS_WARN: Final = 0.05
KS_ALERT: Final = 0.10
RATIO_WARN: Final = (0.67, 1.5)
RATIO_ALERT: Final = (0.5, 2.0)
QUANTILE_GRID: Final = np.linspace(0.0, 1.0, 101)
OTHER: Final = "__other__"
MISSING: Final = "__missing__"

Status = Literal["ok", "warn", "alert", "insufficient_data", "failed"]
FloatArray = npt.NDArray[np.float64]
SEVERITY: Final = {"ok": 0, "insufficient_data": 0, "warn": 1, "alert": 2, "failed": 3}


@dataclass(frozen=True)
class Result:
    value: float | None
    status: Status
    sample_size: int
    reference_size: int | None
    ci: tuple[float, float] | None = None
    thresholds: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- reference profiles


def as_float(values: Sequence[Any]) -> FloatArray:
    """None/NaN → NaN; everything else float."""
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        out[i] = np.nan if v is None else float(v)
    return out


def numeric_profile(values: FloatArray) -> dict[str, Any]:
    present = values[~np.isnan(values)]
    n = len(values)
    if len(present) == 0:
        return {
            "kind": "numeric",
            "n": n,
            "edges": [],
            "props": [0.0, 1.0],
            "quantiles": [],
            "cdf": [],
            "missing_rate": 1.0,
        }
    inner = np.unique(np.quantile(present, np.linspace(0.1, 0.9, 9)))
    counts = bin_counts(values, inner)
    quantiles = np.quantile(present, QUANTILE_GRID)
    ordered = np.sort(present)
    return {
        "kind": "numeric",
        "n": n,
        "edges": [float(e) for e in inner],
        "props": [float(c) / n for c in counts],
        "quantiles": [float(q) for q in quantiles],
        # exact reference CDF at each grid value (ties make it differ from k/100)
        "cdf": [float(c) / len(ordered) for c in np.searchsorted(ordered, quantiles, side="right")],
        "missing_rate": float(np.isnan(values).mean()),
    }


def categorical_profile(values: Sequence[Any], vocabulary: Sequence[str]) -> dict[str, Any]:
    cats = [*vocabulary, OTHER, MISSING]
    counts = categorical_counts(values, vocabulary)
    n = len(values)
    return {"kind": "categorical", "n": n, "categories": cats, "props": [c / n for c in counts]}


def bin_counts(values: FloatArray, inner_edges: Sequence[float] | FloatArray) -> list[int]:
    """Counts per numeric bin: (-inf, e0), [e0, e1), …, [ek, inf), then the missing bin."""
    missing = np.isnan(values)
    idx = np.searchsorted(np.asarray(inner_edges, dtype=np.float64), values[~missing], side="right")
    counts = np.bincount(idx, minlength=len(inner_edges) + 1).tolist()
    return [int(c) for c in counts] + [int(missing.sum())]


def categorical_counts(values: Sequence[Any], vocabulary: Sequence[str]) -> list[int]:
    index = {c: i for i, c in enumerate(vocabulary)}
    counts = [0] * (len(vocabulary) + 2)
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            counts[-1] += 1
        else:
            counts[index.get(str(v), len(vocabulary))] += 1
    return counts


# ---------------------------------------------------------------- statistics


def psi(reference_props: Sequence[float], current_counts: Sequence[int]) -> float:
    n = sum(current_counts)
    if n == 0:
        raise ValueError("empty current sample")
    r = np.maximum(np.asarray(reference_props, dtype=np.float64), EPS)
    c = np.maximum(np.asarray(current_counts, dtype=np.float64) / n, EPS)
    r, c = r / r.sum(), c / c.sum()
    return float(np.sum((c - r) * np.log(c / r)))


def psi_status(value: float) -> Status:
    return "alert" if value >= PSI_ALERT else "warn" if value >= PSI_WARN else "ok"


def numeric_psi(profile: dict[str, Any], values: FloatArray) -> Result:
    n = len(values)
    th = {"warn": PSI_WARN, "alert": PSI_ALERT, "eps": EPS}
    if n < MIN_SAMPLE:
        return Result(None, "insufficient_data", n, profile["n"], thresholds=th)
    counts = bin_counts(values, profile["edges"])
    value = psi(profile["props"], counts)
    return Result(
        value,
        psi_status(value),
        n,
        profile["n"],
        thresholds=th,
        details={
            "bins": len(counts),
            "current_missing_rate": counts[-1] / n,
            "reference_missing_rate": profile.get("missing_rate", profile["props"][-1]),
        },
    )


def categorical_psi(
    profile: dict[str, Any], values: Sequence[Any], vocabulary: Sequence[str]
) -> Result:
    n = len(values)
    th = {"warn": PSI_WARN, "alert": PSI_ALERT, "eps": EPS}
    if n < MIN_SAMPLE:
        return Result(None, "insufficient_data", n, profile["n"], thresholds=th)
    counts = categorical_counts(values, vocabulary)
    value = psi(profile["props"], counts)
    return Result(
        value,
        psi_status(value),
        n,
        profile["n"],
        thresholds=th,
        details={"unseen_rate": counts[-2] / n, "missing_rate": counts[-1] / n},
    )


def ks(profile: dict[str, Any], values: FloatArray) -> Result:
    present = np.sort(values[~np.isnan(values)])
    n, m = len(present), int(profile["n"] * (1 - profile.get("missing_rate", 0.0)))
    critical = KS_C_ALPHA_01 * math.sqrt((n + m) / (n * m)) if n and m else None
    th = {"warn": KS_WARN, "alert": KS_ALERT, "alpha": 0.01, "critical": critical}
    if n < MIN_SAMPLE or not profile["quantiles"]:
        return Result(None, "insufficient_data", n, m, thresholds=th)
    grid = np.asarray(profile["quantiles"], dtype=np.float64)
    f_ref = np.asarray(profile["cdf"], dtype=np.float64)
    f_cur = np.searchsorted(present, grid, side="right") / n
    stat = float(np.max(np.abs(f_cur - f_ref)))
    significant = critical is not None and stat > critical
    status: Status = "ok"
    if significant and stat >= KS_ALERT:
        status = "alert"
    elif significant and stat >= KS_WARN:
        status = "warn"
    return Result(stat, status, n, m, thresholds=th, details={"significant": significant})


def bootstrap_psi_ci(
    profile: dict[str, Any], values: FloatArray, seed: int, resamples: int = BOOTSTRAP
) -> tuple[float, float] | None:
    if len(values) < MIN_SAMPLE:
        return None
    rng = np.random.default_rng(seed)
    stats = [
        psi(
            profile["props"],
            bin_counts(values[rng.integers(0, len(values), len(values))], profile["edges"]),
        )
        for _ in range(resamples)
    ]
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_change(current_k: int, current_n: int, reference_rate: float, reference_n: int) -> Result:
    th = {"ratio_warn": RATIO_WARN, "ratio_alert": RATIO_ALERT}
    if current_n < MIN_SAMPLE:
        return Result(None, "insufficient_data", current_n, reference_n, thresholds=th)
    p = current_k / current_n
    diff = p - reference_rate
    se = math.sqrt(p * (1 - p) / current_n + reference_rate * (1 - reference_rate) / reference_n)
    lo, hi = diff - 1.96 * se, diff + 1.96 * se
    excludes_zero = lo > 0 or hi < 0
    ratio = p / reference_rate if reference_rate > 0 else math.inf
    status: Status = "ok"
    if excludes_zero and not RATIO_ALERT[0] <= ratio <= RATIO_ALERT[1]:
        status = "alert"
    elif excludes_zero and not RATIO_WARN[0] <= ratio <= RATIO_WARN[1]:
        status = "warn"
    return Result(
        p,
        status,
        current_n,
        reference_n,
        ci=wilson(current_k, current_n),
        thresholds=th,
        details={
            "reference_rate": reference_rate,
            "difference": diff,
            "difference_ci": [lo, hi],
            "ratio": ratio if math.isfinite(ratio) else None,
        },
    )


def worst(statuses: Sequence[str]) -> Status:
    if not statuses:
        return "insufficient_data"
    real = [s for s in statuses if s != "insufficient_data"]
    if not real:
        return "insufficient_data"
    top = max(real, key=lambda s: SEVERITY[s])
    return (
        "failed"
        if top == "failed"
        else "alert"
        if top == "alert"
        else ("warn" if top == "warn" else "ok")
    )
