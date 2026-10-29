"""Batch scoring on top of the single-event scoring service.

Every item goes through ``score_transaction`` unchanged, so validation, tenant scoping,
idempotency, point-in-time features, failure handling and the per-item audit record are exactly
those of ``POST /fraud/score``.

Semantics (see docs/api/scoring-api.md):
  * The envelope must hold 1..``max_batch_size`` items (413 above the limit). Each item is
    validated on its own; an invalid item becomes ``invalid`` and does not affect the others.
  * In-batch duplicates (same ``transaction_id``): the first occurrence in *input* order is
    processed. Later identical copies become ``duplicate`` and repeat its outcome; later copies
    with a different payload become ``conflict``.
  * Processing order is ``(event_time, transaction_id)``, so a customer's earlier events are
    committed, and therefore part of the point-in-time history, before their later events are
    scored. The response lists items in input order.
  * Each item commits in its own database transaction (as a single request would). A database or
    unexpected error stops the batch: that item is ``error`` and every item not yet processed is
    ``not_processed``. Items already committed stay committed; retrying the same batch is
    idempotent.
  * Only ``scored`` and ``degraded`` (and duplicates of them) carry a decision. ``degraded`` is
    never ``allow``; every other status carries no decision at all.
"""

from __future__ import annotations

import uuid
from collections import Counter
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from fraud_api.core.errors import APIError
from fraud_api.core.logging import get_logger
from fraud_api.db.models import AuditLog
from fraud_api.observability.metrics import (
    BATCH_ITEMS,
    BATCH_REQUESTS,
    DB_ERRORS,
    SCORING_PATH,
)
from fraud_api.scoring.schemas import ScoreRequest, ScoreResponse
from fraud_api.scoring.service import ScoringContext, score_transaction

log = get_logger(__name__)

PROCESSING_ORDER = "event_time ascending, then transaction_id"


class ItemStatus(StrEnum):
    SCORED = "scored"
    DEGRADED = "degraded"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    CONFLICT = "conflict"
    ERROR = "error"
    NOT_PROCESSED = "not_processed"


class ItemError(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class BatchItemResult(BaseModel):
    index: int = Field(description="Position of the item in the request")
    transaction_id: str | None
    status: ItemStatus
    result: ScoreResponse | None = Field(
        default=None, description="Present only for scored, degraded and duplicate items"
    )
    duplicate_of: int | None = Field(
        default=None, description="Index of the earlier item with the same transaction_id"
    )
    error: ItemError | None = None


class BatchScoreRequest(BaseModel):
    items: list[Any] = Field(
        min_length=1,
        description="Score requests (same schema as POST /api/v1/fraud/score). Validated per item.",
        json_schema_extra={"items": {"$ref": "#/components/schemas/ScoreRequest"}},
    )


class BatchScoreResponse(BaseModel):
    batch_id: uuid.UUID
    received: int
    complete: bool = Field(
        description="False if processing stopped early (database or unexpected error); "
        "items marked not_processed can be retried in a new batch"
    )
    processing_order: str
    summary: dict[str, int]
    items: list[BatchItemResult]


def _validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    # Locations and messages only; submitted values are never echoed.
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


def _transaction_id(raw: Any) -> str | None:
    tid = raw.get("transaction_id") if isinstance(raw, dict) else None
    return tid if isinstance(tid, str) and len(tid) <= 128 else None


def score_batch(
    session: Session,
    ctx: ScoringContext,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    raw_items: list[Any],
    correlation_id: str | None,
    max_items: int,
) -> BatchScoreResponse:
    if len(raw_items) > max_items:
        raise APIError(413, "batch_too_large", f"A batch may contain at most {max_items} items")
    batch_id = uuid.uuid4()
    n = len(raw_items)
    results: list[BatchItemResult | None] = [None] * n

    first_seen: dict[str, tuple[int, str]] = {}
    duplicates: list[tuple[int, int]] = []
    to_process: list[tuple[int, ScoreRequest]] = []
    for i, raw in enumerate(raw_items):
        try:
            req = ScoreRequest.model_validate(raw)
        except ValidationError as exc:
            results[i] = BatchItemResult(
                index=i,
                transaction_id=_transaction_id(raw),
                status=ItemStatus.INVALID,
                error=ItemError(
                    code="validation_error",
                    message="Item failed validation",
                    details=_validation_details(exc),
                ),
            )
            continue
        digest = req.payload_sha256()
        if (seen := first_seen.get(req.transaction_id)) is not None:
            first, first_digest = seen
            if digest == first_digest:
                duplicates.append((i, first))
            else:
                results[i] = BatchItemResult(
                    index=i,
                    transaction_id=req.transaction_id,
                    status=ItemStatus.CONFLICT,
                    duplicate_of=first,
                    error=ItemError(
                        code="duplicate_id_in_batch",
                        message="transaction_id appears earlier in this batch with a "
                        "different payload",
                    ),
                )
            continue
        first_seen[req.transaction_id] = (i, digest)
        to_process.append((i, req))

    # Deterministic point-in-time order: a customer's earlier events commit first.
    to_process.sort(key=lambda item: (item[1].event_time, item[1].transaction_id))
    stopped = False
    path_token = SCORING_PATH.set("batch")
    for i, req in to_process:
        tid = req.transaction_id
        if stopped:
            results[i] = BatchItemResult(
                index=i,
                transaction_id=tid,
                status=ItemStatus.NOT_PROCESSED,
                error=ItemError(
                    code="not_processed",
                    message="Batch stopped before this item; it was not persisted. Retry it.",
                ),
            )
            continue
        try:
            response = score_transaction(session, ctx, org_id, actor_id, req, correlation_id)
            status = ItemStatus.SCORED if response.status == "scored" else ItemStatus.DEGRADED
            results[i] = BatchItemResult(
                index=i, transaction_id=tid, status=status, result=response
            )
        except APIError as exc:
            session.rollback()
            status = ItemStatus.CONFLICT if exc.status_code == 409 else ItemStatus.ERROR
            results[i] = BatchItemResult(
                index=i,
                transaction_id=tid,
                status=status,
                error=ItemError(code=exc.code, message=exc.message),
            )
        except SQLAlchemyError as exc:
            session.rollback()
            DB_ERRORS.labels("api").inc()
            log.error("batch_database_error", error_type=type(exc).__name__)
            stopped = True
            results[i] = BatchItemResult(
                index=i,
                transaction_id=tid,
                status=ItemStatus.ERROR,
                error=ItemError(
                    code="database_error",
                    message="Database error; this item was not persisted. Retry it.",
                ),
            )
        except Exception as exc:
            session.rollback()
            log.exception("batch_unexpected_error", error_type=type(exc).__name__)
            stopped = True
            results[i] = BatchItemResult(
                index=i,
                transaction_id=tid,
                status=ItemStatus.ERROR,
                error=ItemError(code="internal_error", message="Unexpected error; not persisted."),
            )

    SCORING_PATH.reset(path_token)
    for i, first in duplicates:
        primary = results[first]
        assert primary is not None
        carries_result = primary.status in (ItemStatus.SCORED, ItemStatus.DEGRADED)
        results[i] = primary.model_copy(
            update={
                "index": i,
                "status": ItemStatus.DUPLICATE if carries_result else primary.status,
                "duplicate_of": first,
            }
        )

    items = [r for r in results if r is not None]
    assert len(items) == n
    summary = {s.value: 0 for s in ItemStatus} | dict(Counter(r.status.value for r in items))
    for item_status, count in summary.items():
        if count:
            BATCH_ITEMS.labels(item_status).inc(count)
    BATCH_REQUESTS.labels("true" if not stopped else "false").inc()
    try:
        session.add(
            AuditLog(
                organization_id=org_id,
                actor_user_id=actor_id,
                action="fraud.batch_score",
                resource_type="batch",
                resource_id=str(batch_id),
                details={"received": n, "complete": not stopped, **summary},
                correlation_id=correlation_id,
            )
        )
        session.commit()
    except SQLAlchemyError:
        # Item-level outcomes (and their own audit records) are already committed or reported;
        # a missing batch summary record must not hide them from the caller.
        session.rollback()
        log.error("batch_audit_failed", batch_id=str(batch_id))
    return BatchScoreResponse(
        batch_id=batch_id,
        received=n,
        complete=not stopped,
        processing_order=PROCESSING_ORDER,
        summary=summary,
        items=items,
    )
