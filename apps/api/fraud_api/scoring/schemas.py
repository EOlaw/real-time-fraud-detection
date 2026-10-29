from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from pydantic.config import JsonDict

from fraud_ml.domains.card_payments import MERCHANT_CATEGORIES

MerchantCategory = Literal[MERCHANT_CATEGORIES]  # type: ignore[valid-type]


class EventSource(StrEnum):
    """Where an event came from. Replayed and simulated traffic is always labeled as such."""

    LIVE = "live"
    REPLAY = "replay"
    SIMULATED = "simulated"


def _require_tz(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("event_time must include a timezone offset")
    return v.astimezone(UTC)


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]


SCORE_REQUEST_EXAMPLE: JsonDict = {
    "transaction_id": "txn_000123",
    "customer_id": "cus_3f9a0c1d2e4b5a67",
    "merchant_id": "Kirlin and Sons",
    "amount": "84.20",
    "currency": "USD",
    "category": "grocery_pos",
    "event_time": "2020-06-21T12:14:25Z",
    "region": "SC",
    "source": "replay",
}


class ScoreRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_extra={"examples": [SCORE_REQUEST_EXAMPLE]}
    )

    schema_version: Literal["1.0"] = "1.0"
    transaction_id: Identifier
    customer_id: Identifier = Field(description="Tokenized customer/card identifier; never a PAN")
    merchant_id: str | None = Field(default=None, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: Literal["USD"]
    category: MerchantCategory
    event_time: Annotated[datetime, AfterValidator(_require_tz)]
    region: str | None = Field(default=None, max_length=64)
    source: EventSource = EventSource.LIVE

    def payload_sha256(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


Decision = Literal["allow", "challenge", "review", "decline"]


class RuleRef(BaseModel):
    rule_id: uuid.UUID = Field(description="Exact rule version (rule_definitions.id)")
    rule_key: str
    version: int
    action: Literal["flag", "challenge", "review", "decline"]


class RulesSummary(BaseModel):
    status: Literal["none", "evaluated", "error"] = Field(
        description="none = no active rules (decision = policy_decision); error = at least one "
        "rule failed and the decision was escalated to at least the policy's failure decision"
    )
    evaluated: int = Field(description="Number of active rule versions evaluated")
    matched: list[RuleRef]
    errors: list[RuleRef]
    decided_by: RuleRef | None = Field(
        description="Rule that made the final decision stricter than policy_decision, if any"
    )


class RuleEvaluationOut(RuleRef):
    priority: int
    outcome: Literal["matched", "not_matched", "unknown", "error"]
    influenced_decision: bool
    error_type: str | None


class ScoreResponse(BaseModel):
    assessment_id: uuid.UUID
    transaction_id: str
    status: Literal["scored", "degraded"]
    risk_score: float | None
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    decision: Decision = Field(description="Final action: policy_decision escalated by rules")
    policy_decision: Decision = Field(
        description="Decision of the model-bound policy alone (before tenant rules)"
    )
    rules: RulesSummary
    decision_mode: Literal["sandbox"]
    reasons: list[str]
    model_name: str | None
    model_version: str | None
    policy_version: str
    source: EventSource
    processing_time_ms: float
    scored_at: datetime
    idempotent_replay: bool = Field(
        description="True when this response was returned from a previous identical request"
    )


class PredictionDetail(ScoreResponse):
    features: dict[str, float | str | None] | None
    timings_ms: dict[str, float]
    rule_evaluations: list[RuleEvaluationOut] = Field(
        description="Every rule version evaluated for this assessment, in evaluation order"
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
