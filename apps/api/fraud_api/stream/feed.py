"""Per-tenant feed of persisted assessments (source of the SSE endpoint).

Entries are written only *after* the assessment is committed. Redis assigns stream IDs atomically
in insertion order, so a reader that has seen ID X has also been able to see every entry below X;
the ID is the reconnect cursor (``Last-Event-ID``).

Delivery is at least once: a stream worker that crashes after publishing but before acknowledging
re-processes the message (an idempotent replay) and publishes again. Clients de-duplicate by
``assessment_id``. Publishing from the synchronous ``/fraud/score`` and ``/fraud/batch`` paths
is best effort: if Redis is unavailable the score still succeeds and the feed misses that entry.

Only an allowlist of non-sensitive fields is published. Amount, customer and merchant
identifiers, region and feature values are never included.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import redis

from fraud_api.core.logging import get_logger
from fraud_api.observability.metrics import REDIS_ERRORS
from fraud_api.scoring.schemas import ScoreResponse
from fraud_api.stream.keys import StreamKeys

log = get_logger(__name__)

FEED_FIELDS = (
    "assessment_id",
    "transaction_id",
    "status",
    "risk_score",
    "risk_level",
    "decision",
    "policy_decision",  # decision of the model-bound policy before tenant rules (Phase 6)
    "decision_mode",
    "model_version",
    "policy_version",
    "source",
    "scored_at",
)


def feed_payload(
    response: ScoreResponse,
    path: str,
    event_time: datetime,
    ingested_at: datetime | None = None,
    late_arrival: bool | None = None,
) -> dict[str, Any]:
    data = response.model_dump(mode="json")
    out = {k: data[k] for k in FEED_FIELDS}
    out.update(
        {
            "event_time": event_time.isoformat(),
            "path": path,  # "stream" | "api" | "batch"
            "ingested_at": ingested_at.isoformat() if ingested_at else None,
            "late_arrival": late_arrival,
            "idempotent_replay": response.idempotent_replay,
        }
    )
    return out


def publish(
    client: redis.Redis,
    keys: StreamKeys,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
    max_len: int,
) -> str:
    entry_id = client.xadd(
        keys.feed(organization_id),
        {"data": json.dumps(payload, separators=(",", ":"))},
        maxlen=max_len,
        approximate=True,
    )
    return str(entry_id)


def publish_best_effort(
    client: redis.Redis | None,
    keys: StreamKeys,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
    max_len: int,
) -> None:
    if client is None:
        return
    try:
        publish(client, keys, organization_id, payload, max_len)
    except redis.RedisError as exc:
        REDIS_ERRORS.labels("api", "feed_publish").inc()
        log.warning("feed_publish_failed", error_type=type(exc).__name__)
