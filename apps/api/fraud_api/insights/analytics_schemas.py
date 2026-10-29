from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["live", "replay", "simulated", "backfill"]
Basis = Literal["processed", "event"]
Bucket = Literal["hour", "day"]


class WindowOut(BaseModel):
    start: datetime = Field(description="Inclusive, UTC")
    end: datetime = Field(description="Exclusive, UTC")
    basis: Basis = Field(
        description="processed: transactions by received_at, assessments by scored_at; "
        "event: both by the transaction's event_time"
    )
    sources: list[Source] = Field(description="Source labels included")


class Rate(BaseModel):
    numerator: int
    denominator: int
    rate: float | None = Field(description="numerator / denominator; null when denominator is 0")


class TransactionCounts(BaseModel):
    received: int = Field(description="Transactions in the window (scored or imported unscored)")
    scored: int = Field(description="Of those, transactions that have an assessment")
    unscored: int = Field(description="Of those, imported history without an assessment")


class AssessmentCounts(BaseModel):
    total: int
    by_status: dict[str, int]
    by_decision: dict[str, int] = Field(description="Final decision (after rules)")
    by_policy_decision: dict[str, int] = Field(description="Model-bound policy, before rules")
    by_risk_level: dict[str, int]
    by_source: dict[str, int]
    by_rules_status: dict[str, int]


class OverviewOut(BaseModel):
    window: WindowOut
    transactions: TransactionCounts
    assessments: AssessmentCounts
    alert_rate: Rate = Field(description="decision != allow, over assessments")
    policy_alert_rate: Rate = Field(description="policy_decision != allow, over assessments")
    rule_escalation_rate: Rate = Field(description="decision != policy_decision, over assessments")
    degraded_rate: Rate = Field(description="status = degraded, over assessments")


class VolumeBucket(BaseModel):
    start: datetime
    transactions: int
    assessments: int
    alerts: int = Field(description="Assessments with decision != allow")
    by_decision: dict[str, int]


class VolumeOut(BaseModel):
    window: WindowOut
    bucket: Bucket
    buckets: list[VolumeBucket]


class RuleHitRow(BaseModel):
    rule_id: uuid.UUID
    rule_key: str
    version: int
    action: str
    evaluated: int
    matched: int
    unknown: int
    errors: int
    influenced: int = Field(description="Times this version made the decision stricter")
    match_rate: float | None = Field(description="matched / evaluated")


class RuleHitsOut(BaseModel):
    window: WindowOut
    assessments: int = Field(description="Assessments in the window (all denominators below)")
    assessments_with_rules: int = Field(description="rules_status != none")
    assessments_with_match: int
    assessments_escalated: int = Field(description="decision != policy_decision")
    rules: list[RuleHitRow]


class Distribution(BaseModel):
    count: int
    mean: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    max: float | None


class LatencyOut(BaseModel):
    window: WindowOut
    processing_ms: Distribution = Field(
        description="Server-side scoring time per assessment (timings_ms.total): validation to "
        "decision, excluding the final commit and network/queue time"
    )
    components_ms: dict[str, Distribution] = Field(
        description="features, inference, rules (only assessments that recorded the component)"
    )
