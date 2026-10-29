"""Server-Sent Events over the tenant's assessment feed (``GET /api/v1/stream/assessments``).

Event format::

    retry: 3000                          (sent once)
    id: 1761461234567-0                  (Redis stream ID = reconnect cursor)
    event: assessment
    data: {"assessment_id": ..., "decision": ..., ...}   (allowlisted fields only)

    event: reset                         (cursor older than retained history)
    data: {"reason": "cursor_expired", "oldest_available": "..."}

    : keep-alive                         (heartbeat comment)

Resume with ``Last-Event-ID`` (browsers send it automatically) or ``?cursor=``; delivery
continues strictly after that ID. Without either, only events published after connecting are
sent. If entries after the cursor were trimmed (feed retention), a ``reset`` event is sent before
the oldest retained entry, and the client should re-sync from ``GET /api/v1/fraud/assessments``.
The connection closes after ``limit`` events, after ``idle_timeout`` seconds without events, or
after ``FRAUD_SSE_MAX_CONNECTION_SECONDS``. Reconnecting re-authenticates, so revoked or expired
tokens stop receiving data at the next reconnect.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import redis.asyncio as aioredis

from fraud_api.observability.metrics import (
    REDIS_ERRORS,
    SSE_CONNECTIONS,
    SSE_CONNECTIONS_TOTAL,
    SSE_EVENTS,
)
from fraud_api.stream.keys import StreamKeys

CURSOR = re.compile(r"^\d{1,20}-\d{1,20}$")


def _id_tuple(entry_id: str) -> tuple[int, int]:
    ms, seq = entry_id.split("-")
    return int(ms), int(seq)


async def _start_position(
    client: aioredis.Redis, feed: str, cursor: str | None
) -> tuple[str, dict[str, Any] | None]:
    """Resolve the cursor; returns (last_seen_id, reset_event_or_None).

    Retention trims the feed from the head. If the client's cursor entry is gone and older than
    the first retained entry, entries after it may have been trimmed. This is reported as a
    ``reset`` (conservative: it can over-report when exactly the cursor entry was the last one
    trimmed, never under-report). Redis' ``max-deleted-entry-id`` covers explicit ``XDEL``.
    """
    if cursor is None:
        newest = cast("list[tuple[str, Any]]", await client.xrevrange(feed, count=1))
        return (newest[0][0] if newest else "0-0"), None
    try:
        info: dict[str, Any] = await client.xinfo_stream(feed)
    except aioredis.ResponseError:
        return cursor, None  # feed does not exist yet: nothing was trimmed
    first = info.get("first-entry")
    first_id = first[0] if first else None
    deleted = str(info.get("max-deleted-entry-id") or "0-0")
    last_generated = str(info.get("last-generated-id") or "0-0")
    cur = _id_tuple(cursor)
    removed = int(info.get("entries-added") or 0) - int(info.get("length") or 0)
    if cur == (0, 0):  # "from the beginning": a gap only if anything was ever removed
        gap = removed > 0
    elif first_id is None:  # empty: everything ever added was removed
        gap = removed > 0 and cur < _id_tuple(last_generated)
    else:
        still_there = await client.xrange(feed, cursor, cursor)
        gap = (not still_there and cur < _id_tuple(first_id)) or _id_tuple(deleted) > cur
    if gap:
        return cursor, {"reason": "cursor_expired", "oldest_available": first_id}
    return cursor, None


async def assessment_events(
    client: aioredis.Redis,
    keys: StreamKeys,
    organization_id: uuid.UUID,
    cursor: str | None,
    limit: int | None,
    idle_timeout: float | None,
    heartbeat_s: float,
    max_seconds: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    SSE_CONNECTIONS.inc()
    SSE_CONNECTIONS_TOTAL.inc()
    try:
        async for chunk in _events(
            client,
            keys,
            organization_id,
            cursor,
            limit,
            idle_timeout,
            heartbeat_s,
            max_seconds,
            is_disconnected,
        ):
            yield chunk
    except aioredis.RedisError:
        REDIS_ERRORS.labels("api", "sse").inc()
        raise
    finally:
        SSE_CONNECTIONS.dec()


async def _events(
    client: aioredis.Redis,
    keys: StreamKeys,
    organization_id: uuid.UUID,
    cursor: str | None,
    limit: int | None,
    idle_timeout: float | None,
    heartbeat_s: float,
    max_seconds: float,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    feed = keys.feed(organization_id)
    last, reset = await _start_position(client, feed, cursor)
    yield "retry: 3000\n\n"
    if reset is not None:
        SSE_EVENTS.labels("reset").inc()
        yield f"event: reset\ndata: {json.dumps(reset)}\n\n"
    sent = 0
    started = time.monotonic()
    last_event_at = started
    while True:
        now = time.monotonic()
        if now - started >= max_seconds:
            return
        if idle_timeout is not None and now - last_event_at >= idle_timeout:
            return
        if await is_disconnected():
            return
        wait_s = min(heartbeat_s, max_seconds - (now - started))
        if idle_timeout is not None:
            wait_s = min(wait_s, idle_timeout - (now - last_event_at))
        count = 100 if limit is None else min(100, limit - sent)
        resp = cast(
            "list[tuple[str, list[tuple[str, dict[str, str]]]]]",
            await client.xread({feed: last}, count=count, block=max(1, int(wait_s * 1000))),
        )
        entries = resp[0][1] if resp else []
        if not entries:
            SSE_EVENTS.labels("heartbeat").inc()
            yield ": keep-alive\n\n"
            continue
        for entry_id, fields in entries:
            last = entry_id
            sent += 1
            SSE_EVENTS.labels("assessment").inc()
            yield f"id: {entry_id}\nevent: assessment\ndata: {fields.get('data', '{}')}\n\n"
            if limit is not None and sent >= limit:
                return
        last_event_at = time.monotonic()
