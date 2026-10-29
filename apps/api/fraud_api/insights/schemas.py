from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AssessmentRow(BaseModel):
    assessment_id: uuid.UUID
    transaction_id: str
    customer_id: str
    merchant_id: str | None
    amount: float
    currency: str
    category: str
    channel: str
    event_time: datetime
    source: str
    status: Literal["scored", "degraded"]
    risk_score: float | None
    risk_level: str
    decision: str = Field(description="Final action (policy decision escalated by tenant rules)")
    policy_decision: str = Field(description="Decision of the model-bound policy alone")
    rules_status: Literal["none", "evaluated", "error"]
    scored_at: datetime


class AssessmentPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AssessmentRow]


class Contribution(BaseModel):
    feature: str
    value: float | str | None
    contribution_log_odds: float
    normalized: float = Field(
        description="contribution / max |contribution| among the returned features, in [-1, 1]"
    )


class Explanation(BaseModel):
    assessment_id: uuid.UUID
    units: str
    base_value_log_odds: float
    margin_log_odds: float
    probability: float
    stored_risk_score: float | None
    other_features_log_odds: float
    contributions: list[Contribution]


class Distribution(BaseModel):
    feature: str
    title: str
    kind: Literal["categorical", "numeric"]
    bins: list[str]
    edges: list[float] | None = Field(description="numeric bin edges, [lo, hi) per bin")
    shares: list[float]


class DistributionsResponse(BaseModel):
    sample_size: int
    policy_version: str
    thresholds: dict[str, float]
    distributions: list[Distribution]
