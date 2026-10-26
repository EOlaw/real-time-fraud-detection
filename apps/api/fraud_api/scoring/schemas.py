from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

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


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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


class ScoreResponse(BaseModel):
    assessment_id: uuid.UUID
    transaction_id: str
    status: Literal["scored", "degraded"]
    risk_score: float | None
    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    decision: Literal["allow", "challenge", "review", "decline"]
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


def utcnow() -> datetime:
    return datetime.now(UTC)
