"""Decision policy: maps a model score to a risk level and a (sandboxed) action.

Kept separate from the model so thresholds can be reviewed, versioned and audited independently.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Decision(StrEnum):
    ALLOW = "allow"
    CHALLENGE = "challenge"
    REVIEW = "review"
    DECLINE = "decline"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"  # no score (degraded)


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    challenge_at: float = Field(gt=0, le=1)
    review_at: float = Field(gt=0, le=1)
    decline_at: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _monotonic(self) -> Thresholds:
        if not self.challenge_at <= self.review_at <= self.decline_at:
            raise ValueError("thresholds must satisfy challenge_at <= review_at <= decline_at")
        return self


class BoundModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    version: str


class DecisionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    risk_level: RiskLevel
    decision: Decision
    reasons: list[str]


class DecisionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())
    version: str
    domain: str
    mode: Literal["sandbox"]
    model: BoundModel
    thresholds: Thresholds
    on_scoring_failure: Literal["review", "decline"]

    @classmethod
    def load(cls, path: Path) -> DecisionPolicy:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def check_model(self, name: str, version: str) -> None:
        if (self.model.name, self.model.version) != (name, version):
            raise ValueError(
                f"Policy {self.version} is bound to {self.model.name}@{self.model.version}, "
                f"but loaded model is {name}@{version}"
            )

    def decide(self, score: float) -> DecisionOutcome:
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"score out of range: {score}")
        t = self.thresholds
        if score >= t.decline_at:
            level, decision, rule = RiskLevel.CRITICAL, Decision.DECLINE, "decline_at"
        elif score >= t.review_at:
            level, decision, rule = RiskLevel.HIGH, Decision.REVIEW, "review_at"
        elif score >= t.challenge_at:
            level, decision, rule = RiskLevel.MEDIUM, Decision.CHALLENGE, "challenge_at"
        else:
            return DecisionOutcome(
                risk_level=RiskLevel.LOW,
                decision=Decision.ALLOW,
                reasons=[f"score<{t.challenge_at:g}"],
            )
        return DecisionOutcome(
            risk_level=level, decision=decision, reasons=[f"score>={getattr(t, rule):g} ({rule})"]
        )

    def on_failure(self, reason: str) -> DecisionOutcome:
        return DecisionOutcome(
            risk_level=RiskLevel.UNKNOWN,
            decision=Decision(self.on_scoring_failure),
            reasons=[f"scoring_failed: {reason}"],
        )
