"""Operational helpers: stream status, dead-letter inspection and replay.

Dead-letter entries keep the original envelope so an operator can fix the cause and replay. A
replay re-enqueues the envelope on the shard of its customer, marked ``dlq_replay_of=<dlq id>``
(and ``allow_late`` when requested), and removes it from the DLQ in the same ``MULTI``. Replayed
events are processed like any other; idempotency prevents duplicate assessments.
"""

from __future__ import annotations

import json
from typing import Any, cast

import redis

from fraud_api.stream.keys import GROUP, StreamKeys
from fraud_api.stream.messages import MalformedMessage, parse_envelope

DLQ_SUMMARY_FIELDS = (
    "reason",
    "detail",
    "source_shard",
    "source_id",
    "org",
    "transaction_id",
    "attempts",
    "failed_at",
    "worker",
)


def stream_status(client: redis.Redis, keys: StreamKeys) -> dict[str, Any]:
    shards = []
    pipe = client.pipeline(transaction=False)
    for n in range(keys.shards):
        pipe.xlen(keys.shard(n))
        pipe.get(keys.lease(n))
        pipe.pttl(keys.lease(n))
    raw = pipe.execute()
    for n in range(keys.shards):
        backlog, owner, ttl = raw[3 * n : 3 * n + 3]
        pending = 0
        if backlog:
            try:
                summary: dict[str, Any] = client.xpending(keys.shard(n), GROUP)
                pending = int(summary.get("pending", 0))
            except redis.ResponseError:
                pending = 0  # group not created yet
        shards.append(
            {
                "shard": n,
                "backlog": int(backlog),
                "pending": pending,
                "lease_owner": owner,
                "lease_ttl_ms": int(ttl) if owner else None,
            }
        )
    return {
        "shards": shards,
        "backlog_total": sum(s["backlog"] for s in shards),
        "pending_total": sum(s["pending"] for s in shards),
        "dead_letters": int(client.xlen(keys.dlq)),
        "note": "backlog = queued and in-flight messages (processed messages are deleted); "
        "pending = delivered to a worker but not yet acknowledged",
    }


def dlq_list(client: redis.Redis, keys: StreamKeys, count: int = 100) -> list[dict[str, Any]]:
    entries = cast("list[tuple[str, dict[str, str]]]", client.xrange(keys.dlq, count=count))
    return [{"dlq_id": i, **{k: f.get(k, "") for k in DLQ_SUMMARY_FIELDS}} for i, f in entries]


def dlq_replay(
    client: redis.Redis, keys: StreamKeys, ids: list[str] | None, allow_late: bool = False
) -> list[dict[str, Any]]:
    """Replay the given DLQ entries (all if ``ids`` is None)."""
    if ids is None:
        entries = cast("list[tuple[str, dict[str, str]]]", client.xrange(keys.dlq))
    else:
        entries = []
        for i in ids:
            found = cast("list[tuple[str, dict[str, str]]]", client.xrange(keys.dlq, i, i))
            entries.extend(found)
    results: list[dict[str, Any]] = []
    for dlq_id, entry in entries:
        try:
            fields = json.loads(entry.get("envelope", ""))
            env = parse_envelope(fields)
            customer = env.payload.get("customer_id")
            if not isinstance(customer, str):
                raise MalformedMessage("payload has no customer_id")
        except (MalformedMessage, ValueError):
            results.append(
                {"dlq_id": dlq_id, "status": "not_replayable", "reason": entry.get("reason")}
            )
            continue
        fields["allow_late"] = "1" if allow_late else fields.get("allow_late", "0")
        fields["dlq_replay_of"] = dlq_id
        shard = keys.shard_for(env.organization_id, customer)
        pipe = client.pipeline(transaction=True)
        pipe.xadd(keys.shard(shard), cast("dict[Any, Any]", fields))
        pipe.xdel(keys.dlq, dlq_id)
        new_id, _ = pipe.execute()
        results.append(
            {"dlq_id": dlq_id, "status": "requeued", "shard": shard, "message_id": str(new_id)}
        )
    return results


def dlq_delete(client: redis.Redis, keys: StreamKeys, ids: list[str]) -> int:
    return int(client.xdel(keys.dlq, *ids)) if ids else 0
