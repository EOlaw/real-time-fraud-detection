"""ORM models for the Phase 1 vertical slice.

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
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
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
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
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
