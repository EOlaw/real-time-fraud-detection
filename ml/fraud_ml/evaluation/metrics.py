"""Rare-event evaluation metrics. Accuracy is intentionally absent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import numpy.typing as npt
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]


def top_k_metrics(y: IntArray, score: FloatArray, k: int) -> dict[str, float]:
    """Precision/recall when an analyst team can review exactly the ``k`` highest scores."""
    k = max(1, min(k, len(y)))
    top = np.argsort(-score, kind="stable")[:k]
    hits = float(y[top].sum())
    positives = float(y.sum())
    return {
        "k": float(k),
        "precision_at_k": hits / k,
        "recall_at_k": hits / positives if positives else float("nan"),
    }


def threshold_metrics(y: IntArray, score: FloatArray, threshold: float) -> dict[str, float]:
    pred = score >= threshold
    tp = int(np.sum(pred & (y == 1)))
    fp = int(np.sum(pred & (y == 0)))
    fn = int(np.sum(~pred & (y == 1)))
    tn = int(np.sum(~pred & (y == 0)))

    def ratio(a: float, b: float) -> float:
        return a / b if b else float("nan")

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)

    def fbeta(beta: float) -> float:
        b2 = beta * beta
        denom = b2 * precision + recall
        return (1 + b2) * precision * recall / denom if denom and not np.isnan(denom) else 0.0

    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": fbeta(1.0),
        "f2": fbeta(2.0),
        "false_positive_rate": ratio(fp, fp + tn),
        "false_negative_rate": ratio(fn, fn + tp),
        "alert_rate": ratio(tp + fp, len(y)),
    }


def best_fbeta_threshold(y: IntArray, score: FloatArray, beta: float = 2.0) -> float:
    """Threshold maximizing F-beta, searched over observed score values (validation data only)."""
    order = np.argsort(-score, kind="stable")
    s, ys = score[order], y[order]
    tp = np.cumsum(ys)
    fp = np.cumsum(1 - ys)
    # Only evaluate at the last index of each distinct score (ties are all-or-nothing).
    last = np.r_[s[1:] != s[:-1], True]
    tp, fp, s = tp[last], fp[last], s[last]
    precision = tp / (tp + fp)
    recall = tp / max(int(ys.sum()), 1)
    b2 = beta * beta
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(
            precision + recall > 0, (1 + b2) * precision * recall / (b2 * precision + recall), 0.0
        )
    return float(s[int(np.nanargmax(f))])


def operating_points(
    y: IntArray, score: FloatArray, alert_rates: list[float]
) -> list[dict[str, float]]:
    """Threshold/precision/recall at fixed alert budgets (fraction of traffic sent to review)."""
    points = []
    for rate in alert_rates:
        k = max(1, round(rate * len(y)))
        threshold = float(np.sort(score)[::-1][k - 1])
        m = threshold_metrics(y, score, threshold)
        points.append({"target_alert_rate": rate, **m})
    return points


def calibration_table(y: IntArray, score: FloatArray, bins: int = 10) -> dict[str, Any]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    idx = np.clip(np.digitize(score, edges[1:-1]), 0, bins - 1)
    rows, ece = [], 0.0
    for b in range(bins):
        mask = idx == b
        n = int(mask.sum())
        if n == 0:
            continue
        mean_pred, frac_pos = float(score[mask].mean()), float(y[mask].mean())
        ece += n / len(y) * abs(mean_pred - frac_pos)
        rows.append(
            {
                "bin": [float(edges[b]), float(edges[b + 1])],
                "n": n,
                "mean_predicted": mean_pred,
                "observed_rate": frac_pos,
            }
        )
    return {
        "brier": float(brier_score_loss(y, score)),
        "expected_calibration_error": ece,
        "bins": rows,
    }


def cluster_bootstrap_ci(
    y: IntArray,
    score: FloatArray,
    groups: npt.NDArray[Any],
    metric: Callable[[IntArray, FloatArray], float],
    n_resamples: int = 200,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Percentile CI resampling whole customers, since one customer's transactions are correlated
    (a fraud episode is a burst of rows). Row-level bootstrap would understate uncertainty."""
    rng = np.random.default_rng(seed)
    codes = np.unique(groups, return_inverse=True)[1]
    order = np.argsort(codes, kind="stable")
    bounds = np.r_[0, np.flatnonzero(np.diff(codes[order])) + 1, len(codes)]
    members = [order[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]
    stats = []
    for _ in range(n_resamples):
        pick = rng.integers(0, len(members), size=len(members))
        idx = np.concatenate([members[i] for i in pick])
        if y[idx].sum() == 0:
            continue
        stats.append(metric(y[idx], score[idx]))
    lo, hi = np.quantile(stats, [alpha / 2, 1 - alpha / 2])
    return {
        "point": float(metric(y, score)),
        "ci_low": float(lo),
        "ci_high": float(hi),
        "resamples": len(stats),
        "method": "customer-cluster percentile bootstrap",
    }


def ranking_metrics(y: IntArray, score: FloatArray) -> dict[str, float]:
    return {
        "average_precision": float(average_precision_score(y, score)),
        "roc_auc": float(roc_auc_score(y, score)),
        "prevalence": float(y.mean()),
        "n": float(len(y)),
        "positives": float(y.sum()),
    }
