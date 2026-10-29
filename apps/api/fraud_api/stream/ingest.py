"""Event ingestion: validate, label, shard and enqueue (``POST /api/v1/events``).

* Every item is validated as a ``ScoreRequest``; invalid items are reported and never queued.
* The tenant and actor are taken from the bearer token; a body cannot name an organization
  (unknown fields are rejected by the schema).
* Items carry their ``source`` label (``live`` / ``replay`` / ``simulated``) through to the stored
  transaction, plus ingest metadata (``ingested_at``, correlation ID).
* Valid items are routed to ``hash(organization, customer) % shards``. All of a request's valid
  items are enqueued in one Redis ``MULTI`` transaction: either every one is queued or none is.
* Backpressure: if any target shard already holds ``stream_max_backlog`` unprocessed messages,
  nothing is queued and the request fails with 503 ``ingest_backpressure`` (``Retry-After``).

Acceptance (HTTP 202) means "durably queued in Redis", subject to Redis persistence
(``appendfsync everysec``), not "scored". Outcomes arrive on the assessment feed.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import redis
from pydantic import BaseModel, Field, ValidationError

from fraud_api.core.errors import APIError
from fraud_api.observability.metrics import INGEST_EVENTS, REDIS_ERRORS
from fraud_api.scoring.schemas import ScoreRequest
from fraud_api.stream.keys import StreamKeys
from fraud_api.stream.messages import make_envelope


class IngestRequest(BaseModel):
    items: list[Any] = Field(
        min_length=1,
        description="Events (ScoreRequest schema), validated one by one",
        json_schema_extra={"items": {"$ref": "#/components/schemas/ScoreRequest"}},
    )


class IngestItem(BaseModel):
    index: int
    transaction_id: str | None
    status: Literal["queued", "invalid"]
    shard: int | None = None
    message_id: str | None = None
    error: dict[str, Any] | None = None


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    items: list[IngestItem]


def ingest_events(
    client: redis.Redis,
    keys: StreamKeys,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    raw_items: list[Any],
    correlation_id: str,
    max_items: int,
    max_backlog: int,
) -> IngestResponse:
    if len(raw_items) > max_items:
        raise APIError(413, "batch_too_large", f"At most {max_items} events per request")
    results: list[IngestItem] = []
    queue: list[tuple[int, int, dict[Any, Any]]] = []
    for i, raw in enumerate(raw_items):
        try:
            req = ScoreRequest.model_validate(raw)
        except ValidationError as exc:
            tid = raw.get("transaction_id") if isinstance(raw, dict) else None
            results.append(
                IngestItem(
                    index=i,
                    transaction_id=tid if isinstance(tid, str) and len(tid) <= 128 else None,
                    status="invalid",
                    error={
                        "code": "validation_error",
                        "details": [
                            {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                            for e in exc.errors()
                        ],
                    },
                )
            )
            continue
        shard = keys.shard_for(organization_id, req.customer_id)
        envelope = make_envelope(organization_id, actor_id, correlation_id, req)
        queue.append((i, shard, envelope.fields()))
        results.append(
            IngestItem(index=i, transaction_id=req.transaction_id, status="queued", shard=shard)
        )

    if queue:
        try:
            shards = sorted({s for _, s, _ in queue})
            pipe = client.pipeline(transaction=False)
            for s in shards:
                pipe.xlen(keys.shard(s))
            backlog = dict(zip(shards, pipe.execute(), strict=True))
            if any(int(backlog[s]) >= max_backlog for s in shards):
                INGEST_EVENTS.labels("backpressure").inc(len(queue))
                raise APIError(
                    503,
                    "ingest_backpressure",
                    "Stream backlog is full; retry later",
                    headers={"Retry-After": "5"},
                )
            pipe = client.pipeline(transaction=True)  # all-or-nothing enqueue
            for _, s, fields in queue:
                pipe.xadd(keys.shard(s), fields)
            ids = pipe.execute()
        except redis.RedisError as exc:
            REDIS_ERRORS.labels("api", "ingest").inc()
            raise APIError(
                503,
                "stream_unavailable",
                "Event stream unavailable; nothing was queued",
                headers={"Retry-After": "5"},
            ) from exc
        by_index = {i: str(mid) for (i, _, _), mid in zip(queue, ids, strict=True)}
        for item in results:
            if item.status == "queued":
                item.message_id = by_index[item.index]
    accepted = sum(1 for r in results if r.status == "queued")
    INGEST_EVENTS.labels("accepted").inc(accepted)
    INGEST_EVENTS.labels("rejected").inc(len(results) - accepted)
    return IngestResponse(accepted=accepted, rejected=len(results) - accepted, items=results)
