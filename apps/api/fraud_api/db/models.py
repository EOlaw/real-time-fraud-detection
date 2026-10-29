"""ORM models (Phase 1 slice, Phase 4 registry fields, Phase 6 rules, Phase 8 cases and labels).

The full target schema (cases, rules, labels, training runs, ...) is in
docs/architecture/database-design.md; tables are added by migrations as their features ship.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','analyst','viewer','service')", name="ck_users_role"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
        CheckConstraint(
            "status IN ('registered','active','retired')", name="ck_model_versions_status"
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str | None] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    # Summary of the decision policy bound to this version (version, mode, thresholds). Set when
    # a candidate is registered with its policy, and refreshed whenever an API instance activates
    # the version with the policy it loaded.
    bound_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Transaction(Base):
    """One received transaction event. ``external_id`` is the client's transaction id and the
    idempotency key; amounts are exact decimals, never binary floats."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_transactions_org_external"),
        CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        CheckConstraint(
            "source IN ('live','replay','simulated','backfill')", name="ck_transactions_source"
        ),
        # Serves the point-in-time history query for online features.
        Index("ix_transactions_org_customer_time", "organization_id", "customer_id", "event_time"),
        Index("ix_transactions_org_time", "organization_id", "event_time"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    merchant_id: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64))
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = _created_at()


_DECISION_ORDER = "ARRAY['allow','challenge','review','decline']::varchar[]"


class RiskAssessment(Base):
    """Model output + policy decision for one transaction. ``risk_score`` is NULL when scoring was
    degraded; such rows are never presented as low risk."""

    __tablename__ = "risk_assessments"
    __table_args__ = (
        CheckConstraint(
            "risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 1)",
            name="ck_risk_assessments_score_range",
        ),
        CheckConstraint("status IN ('scored','degraded')", name="ck_risk_assessments_status"),
        CheckConstraint(
            "decision IN ('allow','challenge','review','decline')",
            name="ck_risk_assessments_decision",
        ),
        CheckConstraint(
            "status = 'scored' OR decision <> 'allow'",
            name="ck_risk_assessments_degraded_not_allow",
        ),
        CheckConstraint(
            "(status = 'scored') = (risk_score IS NOT NULL)",
            name="ck_risk_assessments_score_iff_scored",
        ),
        # Phase 6: rules can only escalate the model-bound policy decision, never relax it.
        CheckConstraint(
            "policy_decision IN ('allow','challenge','review','decline')",
            name="ck_risk_assessments_policy_decision",
        ),
        CheckConstraint(
            "rules_status IN ('none','evaluated','error')", name="ck_risk_assessments_rules_status"
        ),
        CheckConstraint(
            f"array_position({_DECISION_ORDER}, decision) "
            f">= array_position({_DECISION_ORDER}, policy_decision)",
            name="ck_risk_assessments_rules_never_relax",
        ),
        CheckConstraint(
            "rules_status <> 'none' OR decision = policy_decision",
            name="ck_risk_assessments_no_rules_no_change",
        ),
        CheckConstraint(
            "rules_status <> 'error' OR decision IN ('review','decline')",
            name="ck_risk_assessments_rule_error_not_lenient",
        ),
        Index("ix_risk_assessments_org_scored_at", "organization_id", "scored_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # A probability estimate, not money: binary float is appropriate here.
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    # Final action (policy decision escalated by matched rules); see policy_decision.
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # Decision of the model-bound policy alone, before tenant rules.
    policy_decision: Mapped[str] = mapped_column(String(16), nullable=False)
    # none = no active rules; evaluated = all rules evaluated; error = >= 1 rule failed (fail safe).
    rules_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    decision_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    features: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    timings_ms: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class RuleDefinition(Base):
    """One immutable version of a tenant rule. Rows are append-only (database trigger); a new
    version is a new row. Whether a version is active lives in ``rule_activations``."""

    __tablename__ = "rule_definitions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "rule_key", "version", name="uq_rule_definitions_org_key_version"
        ),
        # Target of the composite foreign keys below (keeps activations/evaluations in-tenant).
        UniqueConstraint(
            "id", "organization_id", "rule_key", name="uq_rule_definitions_id_org_key"
        ),
        CheckConstraint("version > 0", name="ck_rule_definitions_version_positive"),
        CheckConstraint("priority BETWEEN 0 AND 1000", name="ck_rule_definitions_priority"),
        CheckConstraint(
            "action IN ('flag','challenge','review','decline')", name="ck_rule_definitions_action"
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    condition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class RuleActivation(Base):
    """The active version of each rule key (at most one per tenant and key). Changed only by the
    activate/deactivate endpoints, in the same transaction as their audit record."""

    __tablename__ = "rule_activations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rule_definition_id", "organization_id", "rule_key"],
            [
                "rule_definitions.id",
                "rule_definitions.organization_id",
                "rule_definitions.rule_key",
            ],
            name="fk_rule_activations_definition",
            ondelete="RESTRICT",
        ),
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    rule_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    rule_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    activated_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuleEvaluation(Base):
    """Outcome of one rule version for one assessment (append-only). Written in the same database
    transaction as the assessment, so the stored decision and its rule trace always agree."""

    __tablename__ = "rule_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "risk_assessment_id", "rule_definition_id", name="uq_rule_evaluations_assessment_rule"
        ),
        ForeignKeyConstraint(
            ["rule_definition_id", "organization_id", "rule_key"],
            [
                "rule_definitions.id",
                "rule_definitions.organization_id",
                "rule_definitions.rule_key",
            ],
            name="fk_rule_evaluations_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "outcome IN ('matched','not_matched','unknown','error')",
            name="ck_rule_evaluations_outcome",
        ),
        CheckConstraint(
            "NOT influenced_decision OR outcome IN ('matched','error')",
            name="ck_rule_evaluations_influence_needs_match",
        ),
        Index("ix_rule_evaluations_definition", "rule_definition_id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    risk_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT"), nullable=False
    )
    rule_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    influenced_decision: Mapped[bool] = mapped_column(nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------- Phase 8: investigations, labels

CASE_STATUSES = (
    "new",
    "under_review",
    "escalated",
    "resolved_fraud",
    "resolved_legitimate",
    "inconclusive",
)
CLOSED_STATUSES = ("resolved_fraud", "resolved_legitimate", "inconclusive")
_CLOSED_SQL = "status IN ('resolved_fraud','resolved_legitimate','inconclusive')"


class Case(Base):
    """An investigation of one scored transaction. The case status is the investigation's state; it
    never changes the assessment's score or decision, and it is not a fraud label."""

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint("organization_id", "transaction_id", name="uq_cases_org_transaction"),
        CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_cases_severity"),
        CheckConstraint(
            "status IN ('new','under_review','escalated','resolved_fraud','resolved_legitimate',"
            "'inconclusive')",
            name="ck_cases_status",
        ),
        CheckConstraint(
            f"({_CLOSED_SQL}) = (resolved_at IS NOT NULL)", name="ck_cases_resolved_iff_closed"
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= opened_at", name="ck_cases_resolved_after_open"
        ),
        CheckConstraint("version > 0", name="ck_cases_version_positive"),
        Index("ix_cases_queue", "organization_id", "status", "severity", "opened_at"),
        Index("ix_cases_assignee", "organization_id", "assigned_to", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False
    )
    risk_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    opened_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Optimistic locking: every change must name the version it read (409 otherwise).
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CaseEvent(Base):
    """Append-only case history: creation, assignment and status changes (trigger-protected)."""

    __tablename__ = "case_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('created','assigned','status_changed','resolved','reopened')",
            name="ck_case_events_kind",
        ),
        Index("ix_case_events_case", "case_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    from_value: Mapped[str | None] = mapped_column(String(64))
    to_value: Mapped[str | None] = mapped_column(String(64))
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseNote(Base):
    """Append-only analyst note (trigger-protected). Evidence holds references, not uploads."""

    __tablename__ = "case_notes"
    __table_args__ = (
        CheckConstraint("length(body) BETWEEN 1 AND 10000", name="ck_case_notes_body_length"),
        Index("ix_case_notes_case", "case_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FraudLabel(Base):
    """A fraud label with provenance. Append-only (trigger): a correction is a new row whose
    ``supersedes_id`` names the label it replaces, so each transaction has one linear history and
    the current label is the one nothing supersedes. Labels never change scores or decisions."""

    __tablename__ = "fraud_labels"
    __table_args__ = (
        CheckConstraint(
            "label IN ('fraud','legitimate','inconclusive')", name="ck_fraud_labels_label"
        ),
        CheckConstraint(
            "source IN ('analyst_review','chargeback','customer_report','dataset')",
            name="ck_fraud_labels_source",
        ),
        CheckConstraint(
            "confidence IN ('confirmed','probable')", name="ck_fraud_labels_confidence"
        ),
        CheckConstraint(
            "source = 'dataset' OR labeled_by IS NOT NULL", name="ck_fraud_labels_labeled_by"
        ),
        CheckConstraint(
            "source <> 'analyst_review' OR case_id IS NOT NULL",
            name="ck_fraud_labels_review_has_case",
        ),
        # One correction per label (no forks) ...
        UniqueConstraint("supersedes_id", name="uq_fraud_labels_supersedes"),
        # ... and one first label per transaction: together, a single linear history.
        Index(
            "uq_fraud_labels_first_per_txn",
            "organization_id",
            "transaction_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
        Index("ix_labels_txn", "organization_id", "transaction_id", "labeled_at"),
        Index("ix_labels_org_labeled_at", "organization_id", "labeled_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="RESTRICT"))
    labeled_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    # When the outcome became known (e.g. chargeback date), not when the row was written.
    labeled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    supersedes_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fraud_labels.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None] = mapped_column(String(1000))
    # e.g. {"dataset_version": ..., "column": "is_fraud"} for imported dataset labels.
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    recorded_at: Mapped[datetime] = _created_at()


# ---------------------------------------------------------------- Phase 9: monitoring

MONITORING_STATUSES = ("ok", "warn", "alert", "insufficient_data", "failed")
_STATUS_SQL = "('ok','warn','alert','insufficient_data','failed')"
NIL_UUID = uuid.UUID(int=0)


class MonitoringReference(Base):
    """Reference distributions for one model version (append-only, trigger-protected).

    Built from the model's own validation window of its own dataset version, so drift is always
    measured against the data the model's thresholds were chosen on.
    """

    __tablename__ = "monitoring_references"
    __table_args__ = (
        UniqueConstraint(
            "model_version_id", "calc_version", name="uq_monitoring_references_version_calc"
        ),
        CheckConstraint("window_end > window_start", name="ck_monitoring_references_window"),
        CheckConstraint("sample_size > 0", name="ck_monitoring_references_sample"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class MonitoringRun(Base):
    """One monitoring calculation for a scope (organization or global), model version and window.

    Re-running the same scope/model/window/calculation version updates this row and its metrics
    in place (idempotent); acknowledging a warn/alert/failed run is audited.
    """

    __tablename__ = "monitoring_runs"
    __table_args__ = (
        UniqueConstraint(
            "scope_key",
            "model_version_id",
            "window_start",
            "window_end",
            "calc_version",
            name="uq_monitoring_runs_scope_window",
        ),
        CheckConstraint("scope IN ('organization','global')", name="ck_monitoring_runs_scope"),
        CheckConstraint(
            "(scope = 'global') = (organization_id IS NULL)", name="ck_monitoring_runs_scope_org"
        ),
        CheckConstraint(
            "scope_key = coalesce(organization_id, '00000000-0000-0000-0000-000000000000'::uuid)",
            name="ck_monitoring_runs_scope_key",
        ),
        CheckConstraint("window_end > window_start", name="ck_monitoring_runs_window"),
        CheckConstraint(f"status IN {_STATUS_SQL}", name="ck_monitoring_runs_status"),
        CheckConstraint(
            "recommendation IN ('none','review','retraining_review')",
            name="ck_monitoring_runs_recommendation",
        ),
        CheckConstraint(
            "(acknowledged_at IS NULL) = (acknowledged_by IS NULL)",
            name="ck_monitoring_runs_ack",
        ),
        Index("ix_monitoring_runs_model_status", "model_version_id", "status", "window_end"),
        Index("ix_monitoring_runs_org_window", "organization_id", "window_end"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    scope_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    reference_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitoring_references.id", ondelete="RESTRICT"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    labels_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    maturity_days: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_note: Mapped[str | None] = mapped_column(String(1000))


class MonitoringMetric(Base):
    """One metric of a monitoring run. ``labeled`` separates label-matured performance from
    unlabeled distribution and operational signals."""

    __tablename__ = "monitoring_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "metric", "segment", name="uq_monitoring_metrics_run_metric"),
        CheckConstraint(f"status IN {_STATUS_SQL}", name="ck_monitoring_metrics_status"),
        CheckConstraint("sample_size >= 0", name="ck_monitoring_metrics_sample"),
        Index("ix_monitoring_metrics_model_metric", "model_version_id", "metric", "window_end"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitoring_runs.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(96), nullable=False)
    segment: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_size: Mapped[int | None] = mapped_column(Integer)
    labeled: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    calc_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
