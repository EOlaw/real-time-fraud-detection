"""Drift statistics on known distributions and metric-label cardinality (no database)."""

from __future__ import annotations

import math
import re
import uuid

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fraud_api.monitoring import drift
from fraud_api.monitoring.reference import policy_decisions
from fraud_api.observability.endpoint import record_http
from fraud_api.observability.metrics import (
    HTTP_REQUESTS,
    REGISTRY,
    SCORING_PATH,
    WORKER_MESSAGES,
    normalize,
    observe_scoring,
)
from fraud_api.scoring.policy import DecisionPolicy

RNG = np.random.default_rng(42)

# ---------------------------------------------------------------- PSI


def test_psi_identical_is_zero_and_known_value() -> None:
    assert drift.psi([0.25, 0.25, 0.25, 0.25], [25, 25, 25, 25]) == pytest.approx(0.0)
    # Hand-computed: (0.9-0.5)ln(0.9/0.5) + (0.1-0.5)ln(0.1/0.5)
    expected = 0.4 * math.log(1.8) + (-0.4) * math.log(0.2)
    assert drift.psi([0.5, 0.5], [90, 10]) == pytest.approx(expected)


def test_psi_empty_bins_are_floored_not_infinite() -> None:
    value = drift.psi([0.5, 0.5, 0.0], [50, 0, 50])
    assert math.isfinite(value) and value > 1.0
    with pytest.raises(ValueError):
        drift.psi([1.0], [0])


def test_numeric_profile_and_missing_values_are_their_own_bin() -> None:
    ref = RNG.normal(0, 1, 20_000)
    ref[:2_000] = np.nan  # 10 % missing
    profile = drift.numeric_profile(ref)
    assert profile["missing_rate"] == pytest.approx(0.1)
    assert len(profile["edges"]) == 9 and len(profile["props"]) == 11
    assert profile["props"][-1] == pytest.approx(0.1)
    same = RNG.normal(0, 1, 5_000)
    same[:500] = np.nan
    assert drift.numeric_psi(profile, same).status == "ok"
    # Same values, but the missing share jumps from 10 % to 50 %: detected through the missing bin.
    more_missing = same.copy()
    more_missing[:2_500] = np.nan
    r = drift.numeric_psi(profile, more_missing)
    assert r.status == "alert" and r.details["current_missing_rate"] == pytest.approx(0.5)


def test_point_mass_feature_gets_fewer_bins() -> None:
    values = np.concatenate([np.zeros(9_000), RNG.uniform(1, 2, 1_000)])
    profile = drift.numeric_profile(values)
    assert len(profile["edges"]) < 9  # duplicate deciles collapsed
    assert drift.numeric_psi(profile, values[RNG.permutation(len(values))[:2_000]]).status == "ok"


def test_known_shift_is_detected_by_psi_and_ks() -> None:
    profile = drift.numeric_profile(RNG.normal(0, 1, 50_000))
    shifted = RNG.normal(0.5, 1, 5_000)
    psi = drift.numeric_psi(profile, shifted)
    ks = drift.ks(profile, shifted)
    assert psi.value is not None and psi.value > drift.PSI_WARN
    assert ks.value == pytest.approx(0.19, abs=0.03)  # Phi(0.25) - Phi(-0.25)
    assert ks.status == "alert" and ks.details["significant"]
    same = drift.ks(profile, RNG.normal(0, 1, 5_000))
    assert same.status == "ok" and same.value is not None and same.value < 0.05


def test_ks_on_discrete_features_uses_the_exact_reference_cdf() -> None:
    # Regression: a binary flag and a zero-inflated count with identical distributions must not
    # look drifted (the first implementation compared against k/100 and reported KS ~0.7).
    flag = (RNG.random(50_000) < 0.3).astype(np.float64)
    counts = RNG.poisson(0.2, 50_000).astype(np.float64)
    for ref in (flag, counts):
        profile = drift.numeric_profile(ref)
        same = ref[RNG.permutation(len(ref))[:5_000]]
        r = drift.ks(profile, same)
        assert r.value is not None and r.value < 0.03 and r.status == "ok"
    # A real change in the flag's rate is measured as the rate difference.
    shifted = (RNG.random(5_000) < 0.45).astype(np.float64)
    r = drift.ks(drift.numeric_profile(flag), shifted)
    assert r.value == pytest.approx(0.15, abs=0.02) and r.status == "alert"


def test_categorical_unseen_and_missing() -> None:
    vocab = ["a", "b"]
    profile = drift.categorical_profile(["a"] * 500 + ["b"] * 500, vocab)
    assert profile["categories"] == ["a", "b", drift.OTHER, drift.MISSING]
    r = drift.categorical_psi(profile, ["a"] * 400 + ["zzz"] * 100 + [None] * 100, vocab)
    assert r.details == {"unseen_rate": pytest.approx(1 / 6), "missing_rate": pytest.approx(1 / 6)}
    assert r.status == "alert"


def test_insufficient_samples_are_never_ok() -> None:
    profile = drift.numeric_profile(RNG.normal(0, 1, 10_000))
    small = RNG.normal(0, 1, drift.MIN_SAMPLE - 1)
    for r in (
        drift.numeric_psi(profile, small),
        drift.ks(profile, small),
        drift.rate_change(5, drift.MIN_SAMPLE - 1, 0.01, 10_000),
    ):
        assert r.status == "insufficient_data" and r.value is None
    assert drift.bootstrap_psi_ci(profile, small, seed=1) is None
    assert drift.worst(["insufficient_data", "insufficient_data"]) == "insufficient_data"
    assert drift.worst([]) == "insufficient_data"
    assert drift.worst(["ok", "insufficient_data", "warn"]) == "warn"
    assert drift.worst(["warn", "alert", "ok"]) == "alert"


def test_bootstrap_is_deterministic_and_brackets_the_estimate() -> None:
    profile = drift.numeric_profile(RNG.normal(0, 1, 20_000))
    sample = RNG.normal(0.3, 1, 3_000)
    a = drift.bootstrap_psi_ci(profile, sample, seed=7)
    b = drift.bootstrap_psi_ci(profile, sample, seed=7)
    point = drift.numeric_psi(profile, sample).value
    assert a == b and a is not None and point is not None
    assert a[0] <= point <= a[1] + 0.01


def test_rate_change_needs_significance_and_ratio() -> None:
    assert drift.rate_change(100, 10_000, 0.01, 200_000).status == "ok"
    doubled = drift.rate_change(250, 10_000, 0.01, 200_000)
    assert doubled.status == "alert" and doubled.details["ratio"] == pytest.approx(2.5)
    # A large ratio on a tiny count is not significant: no alert.
    assert drift.rate_change(2, 600, 0.001, 200_000).status == "ok"
    lo, hi = drift.wilson(5, 100) or (0, 0)
    assert 0 < lo < 0.05 < hi < 0.12


def test_vectorised_policy_matches_policy_decide(policy_path: object) -> None:
    policy = DecisionPolicy.load(policy_path)  # type: ignore[arg-type]
    scores = np.array([0.0, 0.05, 0.1, 0.3, 0.5, 0.94, 0.95, 1.0])
    expected = [policy.decide(float(s)).decision.value for s in scores]
    assert list(policy_decisions(policy, scores)) == expected


# ---------------------------------------------------------------- metric cardinality

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ALLOWED_LABELS = {
    "le",  # histogram bucket bound (fixed bucket list)
    "method",
    "route",
    "status",
    "path",
    "stage",
    "outcome",
    "decision",
    "complete",
    "component",
    "operation",
    "result",
    "event",
    "model",
    "version",
    "policy",
}


def _route_values() -> set[str]:
    return {
        s.labels["route"]
        for metric in REGISTRY.collect()
        if metric.name == "fraud_http_requests"
        for s in metric.samples
    }


def test_http_metrics_use_route_templates_not_paths() -> None:
    app = FastAPI()
    app.middleware("http")(record_http)

    @app.get("/things/{thing_id}")
    def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    client = TestClient(app)
    for _ in range(50):
        client.get(f"/things/{uuid.uuid4()}?q={uuid.uuid4()}")
        client.get(f"/unknown/{uuid.uuid4()}")
    client.request("BREW", "/things/x")
    routes = _route_values()
    assert "/things/{thing_id}" in routes and "unmatched" in routes
    assert not any(UUID_RE.search(r) for r in routes)
    methods = {
        s.labels["method"]
        for m in REGISTRY.collect()
        if m.name == "fraud_http_requests"
        for s in m.samples
    }
    assert "BREW" not in methods and "other" in methods
    assert HTTP_REQUESTS.labels("GET", "/things/{thing_id}", "200")._value.get() >= 50


def test_every_metric_label_is_from_the_allowlist() -> None:
    token = SCORING_PATH.set("not-a-path")
    try:
        observe_scoring("weird-status", "maybe", False, True, {"features": 1.0, "secret": 2.0})
    finally:
        SCORING_PATH.reset(token)
    WORKER_MESSAGES.labels(normalize("cus_1234", {"scored"})).inc()
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            assert set(sample.labels) <= ALLOWED_LABELS, (metric.name, sample.labels)
            for value in sample.labels.values():
                assert not UUID_RE.search(value) and "cus_" not in value
                assert len(value) <= 80
    stages = {
        s.labels.get("stage")
        for m in REGISTRY.collect()
        if m.name == "fraud_scoring_duration_seconds"
        for s in m.samples
    }
    assert "secret" not in stages
