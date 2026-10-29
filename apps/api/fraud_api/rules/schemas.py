from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RULE_KEY_PATTERN = r"^[a-z0-9][a-z0-9_.\-]{0,63}$"
RuleKey = Annotated[str, Field(pattern=RULE_KEY_PATTERN)]
RuleAction = Literal["flag", "challenge", "review", "decline"]


class RuleCreate(BaseModel):
    """A new version of ``rule_key`` (version 1 if the key is new). Created inactive."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "rule_key": "large_night_cnp",
                    "name": "Large card-not-present payment at night",
                    "priority": 500,
                    "action": "review",
                    "condition": {
                        "all": [
                            {"field": "channel", "op": "eq", "value": "card_not_present"},
                            {"field": "amount", "op": "gte", "value": 900},
                            {"field": "feature.is_night", "op": "eq", "value": 1},
                        ]
                    },
                }
            ]
        },
    )
    rule_key: RuleKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    priority: int = Field(default=100, ge=0, le=1000, description="Higher is evaluated first")
    action: RuleAction
    condition: dict[str, Any] = Field(description="JSON condition; see GET /api/v1/rules/grammar")


class RuleVersionOut(BaseModel):
    id: uuid.UUID
    rule_key: str
    version: int
    name: str
    description: str | None
    priority: int
    action: RuleAction
    condition: dict[str, Any]
    condition_sha256: str
    created_by: uuid.UUID
    created_at: datetime
    active: bool


class RuleSummaryOut(BaseModel):
    rule_key: str
    latest_version: int
    versions: int
    active_version: int | None
    active: RuleVersionOut | None
    activated_at: datetime | None


class RuleListOut(BaseModel):
    active_count: int
    max_active: int
    items: list[RuleSummaryOut]


class RuleDetailOut(BaseModel):
    rule_key: str
    active_version: int | None
    activated_at: datetime | None
    versions: list[RuleVersionOut] = Field(description="Newest first")


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_active_version: int | Literal["none"] | None = Field(
        default=None,
        description="Optional compare-and-set guard: the version you believe is active now "
        "('none' if no version is active). 409 activation_conflict if it differs.",
    )


class ActivationOut(BaseModel):
    rule_key: str
    active_version: int | None
    previous_version: int | None
    changed: bool
    activated_at: datetime | None
