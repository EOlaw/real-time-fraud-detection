"""Fraud labels with provenance.

A label records an *observation* about a transaction: who or what said it was fraud, legitimate
or inconclusive, how confident they were, and when the outcome became known (``labeled_at``).
Analyst dispositions are observations from a reviewed (selected, not random) population; dataset
labels are the synthetic dataset's ``is_fraud`` column. Neither is automatically ground truth.

History is append-only. The first label of a transaction has no ``supersedes_id``; a correction
must name the current label in ``supersedes_id`` (compare-and-set), so concurrent corrections
of the same label conflict (409) instead of forking or overwriting. The database enforces both
rules with unique indexes, and a trigger forbids UPDATE and DELETE.

Labels never touch assessments: a label cannot change a score, a policy decision or a final
decision, and it is never a model input.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from fraud_api.cases.schemas import LabelCreate, LabelHistory, LabelOut, UserRef
from fraud_api.core.errors import APIError
from fraud_api.core.logging import get_logger
from fraud_api.core.security import Principal
from fraud_api.db.models import AuditLog, Case, FraudLabel, Transaction, User

log = get_logger(__name__)

CLOCK_SKEW: Final = timedelta(minutes=5)


def database_unavailable(exc: SQLAlchemyError) -> APIError:
    log.error("database_error", error_type=type(exc).__name__)
    return APIError(
        503,
        "database_unavailable",
        "The database is unavailable; nothing was changed. Retry the request.",
        headers={"Retry-After": "5"},
    )


def _users(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    return dict(session.execute(select(User.id, User.email).where(User.id.in_(ids))).all())


def _history_rows(session: Session, org_id: uuid.UUID, txn_id: uuid.UUID) -> list[FraudLabel]:
    return list(
        session.scalars(
            select(FraudLabel)
            .where(FraudLabel.organization_id == org_id, FraudLabel.transaction_id == txn_id)
            .order_by(FraudLabel.id)
        )
    )


def _current(rows: list[FraudLabel]) -> FraudLabel | None:
    superseded = {r.supersedes_id for r in rows if r.supersedes_id is not None}
    heads = [r for r in rows if r.id not in superseded]
    return heads[-1] if heads else None


def build_history(session: Session, org_id: uuid.UUID, txn: Transaction) -> LabelHistory:
    rows = _history_rows(session, org_id, txn.id)
    emails = _users(session, {r.labeled_by for r in rows if r.labeled_by})
    superseded_by = {r.supersedes_id: r.id for r in rows if r.supersedes_id is not None}
    head = _current(rows)
    out = [
        LabelOut(
            id=r.id,
            transaction_id=txn.external_id,
            label=r.label,
            source=r.source,
            confidence=r.confidence,
            case_id=r.case_id,
            labeled_by=UserRef(id=r.labeled_by, email=emails.get(r.labeled_by, "unknown"))
            if r.labeled_by
            else None,
            labeled_at=r.labeled_at,
            recorded_at=r.recorded_at,
            supersedes_id=r.supersedes_id,
            superseded_by=superseded_by.get(r.id),
            current=head is not None and r.id == head.id,
            reason=r.reason,
            provenance=r.provenance,
        )
        for r in rows
    ]
    return LabelHistory(
        transaction_id=txn.external_id,
        current=next((o for o in out if o.current), None),
        history=out,
    )


def _transaction(session: Session, org_id: uuid.UUID, external_id: str) -> Transaction:
    txn = session.scalar(
        select(Transaction).where(
            Transaction.organization_id == org_id, Transaction.external_id == external_id
        )
    )
    if txn is None:
        # Same answer for "missing" and "another tenant's": no cross-tenant probing.
        raise APIError(404, "not_found", "Transaction not found")
    return txn


def label_history(session: Session, org_id: uuid.UUID, external_id: str) -> LabelHistory:
    return build_history(session, org_id, _transaction(session, org_id, external_id))


def add_label(
    session: Session,
    principal: Principal,
    body: LabelCreate,
    correlation_id: str | None,
    *,
    commit: bool = True,
) -> LabelOut:
    """Validate and append a label (optionally inside a caller's transaction)."""
    org_id = principal.organization_id
    txn = _transaction(session, org_id, body.transaction_id)
    if body.labeled_at.tzinfo is None:
        raise APIError(422, "invalid_label", "labeled_at must include a timezone offset")
    labeled_at = body.labeled_at.astimezone(UTC)
    if labeled_at > datetime.now(UTC) + CLOCK_SKEW:
        raise APIError(422, "invalid_label", "labeled_at is in the future")
    if labeled_at < txn.event_time:
        raise APIError(422, "invalid_label", "labeled_at is before the transaction happened")
    if body.source == "analyst_review" and body.case_id is None:
        raise APIError(422, "invalid_label", "analyst_review labels must reference a case")
    if body.case_id is not None:
        case = session.scalar(
            select(Case).where(Case.id == body.case_id, Case.organization_id == org_id)
        )
        if case is None:
            raise APIError(404, "not_found", "Case not found")
        if case.transaction_id != txn.id:
            raise APIError(422, "invalid_label", "The case is for a different transaction")

    rows = _history_rows(session, org_id, txn.id)
    head = _current(rows)
    if head is None and body.supersedes_id is not None:
        raise APIError(409, "label_conflict", "The transaction has no label to supersede")
    if head is not None:
        if body.supersedes_id != head.id:
            raise APIError(
                409,
                "label_conflict",
                f"The current label is {head.id}; a new label must supersede it explicitly "
                "(set supersedes_id). History is never overwritten.",
            )
        if not body.reason:
            raise APIError(422, "invalid_label", "A correction needs a reason")
        if labeled_at < head.labeled_at:
            raise APIError(
                422, "invalid_label", "A correction cannot be dated before the label it replaces"
            )
    label = FraudLabel(
        organization_id=org_id,
        transaction_id=txn.id,
        label=body.label,
        source=body.source,
        confidence=body.confidence,
        case_id=body.case_id,
        labeled_by=principal.user_id,
        labeled_at=labeled_at,
        supersedes_id=body.supersedes_id,
        reason=body.reason,
        provenance={"recorded_via": "api"},
    )
    session.add(label)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        # Another writer appended a first label or a correction of the same label concurrently.
        raise APIError(
            409, "label_conflict", "The label history changed concurrently; reload and retry"
        ) from exc
    session.add(
        AuditLog(
            organization_id=org_id,
            actor_user_id=principal.user_id,
            action="label.supersede" if body.supersedes_id else "label.create",
            resource_type="fraud_label",
            resource_id=str(label.id),
            details={
                "transaction_id": txn.external_id,
                "label": body.label,
                "source": body.source,
                "confidence": body.confidence,
                "case_id": str(body.case_id) if body.case_id else None,
                "supersedes_id": body.supersedes_id,
                "labeled_at": labeled_at.isoformat(),
            },
            correlation_id=correlation_id,
        )
    )
    if commit:
        session.commit()
    history = build_history(session, org_id, txn)
    return next(o for o in history.history if o.id == label.id)


def create_label(
    session: Session, principal: Principal, body: LabelCreate, correlation_id: str | None
) -> LabelOut:
    try:
        return add_label(session, principal, body, correlation_id)
    except SQLAlchemyError as exc:
        session.rollback()
        raise database_unavailable(exc) from exc


def import_dataset_labels(
    session: Session, org_id: uuid.UUID, df: pd.DataFrame, manifest: dict[str, Any]
) -> dict[str, int]:
    """Record the dataset's ``is_fraud`` column as ``source='dataset'`` labels.

    Only transactions already stored for the organization are labeled; transactions that already
    have any label are skipped (idempotent, and never overwrites analyst or chargeback labels).
    The dataset carries no label timing, so ``labeled_at`` is the import time and the provenance
    says so; maturity is measured from ``event_time`` (see docs/ml/model-development-plan.md).
    """
    ids = {str(i): int(v) for i, v in zip(df["event_id"], df["is_fraud"], strict=True)}
    present: dict[str, uuid.UUID] = {}
    labeled: set[uuid.UUID] = set()
    keys = list(ids)
    for k in range(0, len(keys), 5000):
        chunk = dict(
            session.execute(
                select(Transaction.external_id, Transaction.id).where(
                    Transaction.organization_id == org_id,
                    Transaction.external_id.in_(keys[k : k + 5000]),
                )
            ).all()
        )
        present.update(chunk)
        labeled.update(
            session.scalars(
                select(FraudLabel.transaction_id).where(
                    FraudLabel.organization_id == org_id,
                    FraudLabel.transaction_id.in_(list(chunk.values())),
                )
            )
        )
    now = datetime.now(UTC)
    provenance = {
        "column": "is_fraud",
        "dataset_version": manifest.get("dataset_version"),
        "note": "synthetic dataset label; true label timing unknown, labeled_at = import time",
    }
    created = 0
    for ext, txn_id in present.items():
        if txn_id in labeled:
            continue
        session.add(
            FraudLabel(
                organization_id=org_id,
                transaction_id=txn_id,
                label="fraud" if ids[ext] == 1 else "legitimate",
                source="dataset",
                confidence="confirmed",
                labeled_by=None,
                labeled_at=now,
                provenance=provenance,
            )
        )
        created += 1
    session.add(
        AuditLog(
            organization_id=org_id,
            action="label.import_dataset",
            resource_type="fraud_label",
            details={
                "created": created,
                "already_labeled": len(labeled),
                "not_in_database": len(ids) - len(present),
                "dataset_version": manifest.get("dataset_version"),
            },
        )
    )
    session.commit()
    return {
        "created": created,
        "already_labeled": len(labeled),
        "not_in_database": len(ids) - len(present),
    }
