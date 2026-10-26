from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from fraud_api.scoring.policy import Decision, DecisionPolicy, RiskLevel


def _policy(**overrides: Any) -> DecisionPolicy:
    data: dict[str, Any] = {
        "version": "p1",
        "domain": "card_payments",
        "mode": "sandbox",
        "model": {"name": "m", "version": "1"},
        "thresholds": {"challenge_at": 0.1, "review_at": 0.5, "decline_at": 0.9},
        "on_scoring_failure": "review",
    }
    data.update(overrides)
    return DecisionPolicy.model_validate(data)


@pytest.mark.parametrize(
    ("score", "level", "decision"),
    [
        (0.0, RiskLevel.LOW, Decision.ALLOW),
        (0.0999, RiskLevel.LOW, Decision.ALLOW),
        (0.1, RiskLevel.MEDIUM, Decision.CHALLENGE),
        (0.5, RiskLevel.HIGH, Decision.REVIEW),
        (0.8999, RiskLevel.HIGH, Decision.REVIEW),
        (0.9, RiskLevel.CRITICAL, Decision.DECLINE),
        (1.0, RiskLevel.CRITICAL, Decision.DECLINE),
    ],
)
def test_bands_are_inclusive_lower_bounds(
    score: float, level: RiskLevel, decision: Decision
) -> None:
    out = _policy().decide(score)
    assert (out.risk_level, out.decision) == (level, decision)
    assert out.reasons


def test_rejects_non_monotonic_thresholds() -> None:
    with pytest.raises(ValidationError, match="challenge_at <= review_at <= decline_at"):
        _policy(thresholds={"challenge_at": 0.6, "review_at": 0.5, "decline_at": 0.9})


def test_failure_decision_can_never_be_allow() -> None:
    with pytest.raises(ValidationError):
        _policy(on_scoring_failure="allow")
    out = _policy().on_failure("model_unavailable")
    assert out.decision is Decision.REVIEW and out.risk_level is RiskLevel.UNKNOWN


def test_only_sandbox_mode_is_supported() -> None:
    with pytest.raises(ValidationError):
        _policy(mode="production")


def test_out_of_range_score_is_an_error() -> None:
    with pytest.raises(ValueError, match="out of range"):
        _policy().decide(1.5)
    with pytest.raises(ValueError, match="out of range"):
        _policy().decide(float("nan"))


def test_policy_is_bound_to_model_version() -> None:
    _policy().check_model("m", "1")
    with pytest.raises(ValueError, match="bound to"):
        _policy().check_model("m", "2")


def test_repo_policy_file_is_valid() -> None:
    from pathlib import Path

    policy = DecisionPolicy.load(Path("ml/configs/decision_policy.yaml"))
    assert policy.mode == "sandbox"
