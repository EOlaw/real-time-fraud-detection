from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CaseStatus = Literal[
    "new", "under_review", "escalated", "resolved_fraud", "resolved_legitimate", "inconclusive"
]
Severity = Literal["low", "medium", "high", "critical"]
LabelValue = Literal["fraud", "legitimate", "inconclusive"]
LabelSource = Literal["analyst_review", "chargeback", "customer_report", "dataset"]
Confidence = Literal["confirmed", "probable"]


class UserRef(BaseModel):
    id: uuid.UUID
    email: str


class LabelOut(BaseModel):
    id: int
    transaction_id: str
    label: LabelValue
    source: LabelSource
    confidence: Confidence
    case_id: uuid.UUID | None
    labeled_by: UserRef | None = Field(description="Who recorded it; null only for dataset labels")
    labeled_at: datetime = Field(description="When the outcome became known")
    recorded_at: datetime = Field(description="When the row was written")
    supersedes_id: int | None
    superseded_by: int | None
    current: bool
    reason: str | None
    provenance: dict[str, object]


class LabelHistory(BaseModel):
    transaction_id: str
    current: LabelOut | None
    history: list[LabelOut] = Field(description="Oldest first; nothing is ever overwritten")


class LabelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: str = Field(min_length=1, max_length=128, description="Client transaction id")
    label: LabelValue
    source: Literal["analyst_review", "chargeback", "customer_report"] = Field(
        description="Dataset labels are imported by the CLI, never through the API"
    )
    confidence: Confidence
    labeled_at: datetime = Field(description="When the outcome became known (timezone required)")
    case_id: uuid.UUID | None = None
    supersedes_id: int | None = Field(
        default=None, description="Required when the transaction already has a label"
    )
    reason: str | None = Field(default=None, max_length=1000)


class CaseSummary(BaseModel):
    id: uuid.UUID
    transaction_id: str
    assessment_id: uuid.UUID
    source: str
    severity: Severity
    status: CaseStatus
    assigned_to: UserRef | None
    opened_by: UserRef
    opened_at: datetime
    resolved_at: datetime | None
    updated_at: datetime
    version: int
    assessment_status: Literal["scored", "degraded"]
    risk_score: float | None = Field(description="Model probability (never changed by a case)")
    policy_decision: str
    decision: str = Field(description="Final decision after rules (never changed by a case)")
    current_label: LabelValue | None


class CasePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CaseSummary]


class CaseEventOut(BaseModel):
    id: int
    kind: Literal["created", "assigned", "status_changed", "resolved", "reopened"]
    from_value: str | None
    to_value: str | None
    actor: UserRef
    case_version: int
    created_at: datetime


class CaseNoteOut(BaseModel):
    id: int
    author: UserRef
    body: str
    evidence: list[str]
    created_at: datetime


class TransactionInfo(BaseModel):
    transaction_id: str
    customer_id: str = Field(description="Tokenized; never a card number")
    merchant_id: str | None
    amount: float
    currency: str
    category: str
    channel: str
    event_time: datetime
    source: str


class ModelInfo(BaseModel):
    name: str | None
    version: str | None
    registry_status: str | None
    serving: bool = Field(description="Whether this API instance serves the scoring model")


class CaseDetail(CaseSummary):
    transaction: TransactionInfo
    model: ModelInfo
    events: list[CaseEventOut]
    notes: list[CaseNoteOut]
    labels: LabelHistory
    allowed_transitions: list[CaseStatus] = Field(
        description="Statuses the caller may move this case to now (role and assignment aware)"
    )


class CaseOpened(BaseModel):
    created: bool = Field(description="False when the transaction already had a case")
    case: CaseDetail


class CaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessment_id: uuid.UUID
    note: str | None = Field(default=None, min_length=1, max_length=10000)


class VersionGuard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, description="The case version you read (409 if stale)")


class AssignRequest(VersionGuard):
    assignee_id: uuid.UUID | None = Field(description="null releases the case")


class StatusRequest(VersionGuard):
    status: Literal["under_review", "escalated"] = Field(
        description="Open statuses only; closing goes through /resolve. Reopening a closed case "
        "(admin) uses under_review."
    )


class ResolveRequest(VersionGuard):
    outcome: LabelValue
    note: str | None = Field(default=None, min_length=1, max_length=10000)
    record_label: bool = Field(
        default=False,
        description="Also record an analyst_review label for the transaction (refused if the "
        "transaction already has a label; correct it through /labels instead)",
    )
    label_confidence: Confidence = "confirmed"


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)
    evidence: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20, description="References (ids, URLs), not uploads"
    )


class Assignee(BaseModel):
    id: uuid.UUID
    email: str
    role: Literal["admin", "analyst"]
