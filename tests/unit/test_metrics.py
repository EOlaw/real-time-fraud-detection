from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import average_precision_score

from fraud_ml.evaluation.metrics import (
    best_fbeta_threshold,
    calibration_table,
    cluster_bootstrap_ci,
    operating_points,
    threshold_metrics,
    top_k_metrics,
)

Y = np.array([1, 0, 1, 0, 0, 0, 1, 0, 0, 0])
S = np.array([0.9, 0.8, 0.7, 0.3, 0.2, 0.2, 0.1, 0.05, 0.01, 0.0])


def test_top_k() -> None:
    m = top_k_metrics(Y, S, k=3)
    assert m["precision_at_k"] == pytest.approx(2 / 3)
    assert m["recall_at_k"] == pytest.approx(2 / 3)


def test_threshold_confusion_matrix() -> None:
    m = threshold_metrics(Y, S, 0.7)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (2, 1, 1, 6)
    assert m["false_positive_rate"] == pytest.approx(1 / 7)
    assert m["false_negative_rate"] == pytest.approx(1 / 3)
    assert m["alert_rate"] == pytest.approx(0.3)


def test_best_fbeta_threshold_is_an_observed_score() -> None:
    t = best_fbeta_threshold(Y, S, beta=2.0)
    assert t in S
    best = threshold_metrics(Y, S, t)["f2"]
    assert all(threshold_metrics(Y, S, float(s))["f2"] <= best + 1e-12 for s in S)


def test_operating_points_respect_alert_budget() -> None:
    (op,) = operating_points(Y, S, [0.2])
    assert op["alert_rate"] == pytest.approx(0.2)


def test_constant_scorer_ap_equals_prevalence() -> None:
    rng = np.random.default_rng(0)
    y = (rng.random(5000) < 0.01).astype(int)
    assert average_precision_score(y, np.full(5000, 0.01)) == pytest.approx(y.mean())


def test_calibration_of_perfectly_calibrated_scores() -> None:
    rng = np.random.default_rng(1)
    p = rng.random(200_000)
    y = (rng.random(200_000) < p).astype(int)
    assert calibration_table(y, p)["expected_calibration_error"] < 0.01


def test_cluster_bootstrap_interval_contains_point() -> None:
    rng = np.random.default_rng(2)
    groups = np.repeat(np.arange(50), 40)
    y = (rng.random(2000) < 0.05).astype(int)
    s = np.clip(y * 0.5 + rng.random(2000) * 0.6, 0, 1)
    ci = cluster_bootstrap_ci(
        y, s, groups, lambda a, b: float(average_precision_score(a, b)), n_resamples=100, seed=0
    )
    assert ci["ci_low"] <= ci["point"] <= ci["ci_high"]
    assert ci["ci_high"] - ci["ci_low"] > 0
