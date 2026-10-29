"""Stream scoring worker.

Run:  uv run python -m fraud_api.stream.worker --worker-id w1 [--shards 0-3]

Per shard, exactly one worker holds a lease (``SET NX PX``), renewed atomically before every
message (compare-and-``PEXPIRE``), so a long backlog never outlives the lease. On acquiring a
shard the worker claims the shard's pending messages from whoever held it before (``XAUTOCLAIM``),
then processes **pending messages first, then new ones, strictly in stream order**. A message
being retried blocks the rest of its shard, which preserves per-customer order.

Per message:
  1. Parse the envelope and re-validate the payload. Malformed or invalid → dead letter
     (permanent; no retries).
  2. Late check: if the customer already has a stored event *later* than this one, the event is a
     late arrival. It is scored against the history stored so far and flagged
     ``late_arrival: true`` on the feed; later assessments are not re-scored. If it is older than
     the customer's latest stored event by more than ``stream_late_tolerance_seconds`` (and was
     not explicitly replayed with ``allow_late``) → dead letter ``late_beyond_tolerance``.
  3. ``score_transaction``: the same single path as ``POST /fraud/score`` (features, model,
     policy, persistence, audit; idempotent on (organization, transaction_id)).
  4. After the database commit, in one Redis ``MULTI``: publish to the tenant feed, ``XACK``,
     ``XDEL`` the message and clear its attempt counter.

Delivery is **at least once**. A crash after the commit but before step 4 redelivers the message;
the second run is an idempotent replay (same assessment, no duplicate rows) and publishes again.
Nothing here is exactly once.

Failures:
  * 409 conflicts (``idempotency_conflict``, ``transaction_exists_unscored``) → dead letter.
  * Transient errors (database, Redis, ``scoring_unavailable`` in reject mode, unexpected
    exceptions) → retried with exponential backoff, at most ``max_attempts`` attempts in total
    across worker restarts (counter kept in Redis) → dead letter ``retries_exhausted``.
  * A message whose stored attempt count is already at the limit (for example a worker crash loop)
    → dead letter ``attempts_exhausted`` before any further attempt.
  A failure never produces an assessment, so it can never become ``allow``. With the model
  missing and ``manual_review`` mode, scoring succeeds as ``degraded`` / ``review``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import redis
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from fraud_api.core.config import Settings, get_settings
from fraud_api.core.errors import APIError
from fraud_api.core.logging import configure_logging, get_logger
from fraud_api.db.models import Transaction
from fraud_api.db.session import make_engine, make_session_factory
from fraud_api.observability.metrics import (
    DB_ERRORS,
    MODEL_INFO,
    REDIS_ERRORS,
    REGISTRY,
    SCORING_PATH,
    WORKER_MESSAGES,
    WORKER_OUTCOMES,
    normalize,
)
from fraud_api.scoring.context import build_scoring_context
from fraud_api.scoring.service import ScoringContext, score_transaction
from fraud_api.stream.feed import feed_payload
from fraud_api.stream.keys import GROUP, StreamKeys
from fraud_api.stream.messages import Envelope, InvalidPayload, MalformedMessage, parse_envelope

log = get_logger(__name__)

# Fixed, data-free explanations recorded with dead letters (exception text is never stored).
DLQ_REASONS = {
    "malformed_message": "Envelope could not be parsed",
    "invalid_payload": "Payload failed ScoreRequest validation",
    "late_beyond_tolerance": "Event is older than the customer's latest stored event by more "
    "than the late-arrival tolerance",
    "idempotency_conflict": "transaction_id already scored with a different payload",
    "transaction_exists_unscored": "transaction_id exists as imported, unscored history",
    "retries_exhausted": "Transient failures persisted for the maximum number of attempts",
    "attempts_exhausted": "Attempt limit reached before processing (e.g. repeated worker crashes)",
}

RENEW_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
RELEASE_LEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

ScoreFn = Callable[..., Any]


@dataclass(frozen=True)
class WorkerConfig:
    max_attempts: int = 5
    backoff_base_s: float = 0.2
    backoff_max_s: float = 5.0
    lease_ms: int = 15_000
    block_ms: int = 1_000
    read_count: int = 100
    late_tolerance_s: int = 7 * 24 * 3600
    feed_max_len: int = 100_000


@dataclass
class _Transient(Exception):
    reason: str


@dataclass
class WorkerStats:
    outcomes: Counter[str] = field(default_factory=Counter)

    def add(self, outcome: str) -> None:
        self.outcomes[outcome] += 1
        WORKER_MESSAGES.labels(normalize(outcome, WORKER_OUTCOMES)).inc()


class StreamWorker:
    def __init__(
        self,
        client: redis.Redis,
        keys: StreamKeys,
        session_factory: sessionmaker[Session],
        ctx: ScoringContext,
        cfg: WorkerConfig,
        worker_id: str,
        shards: Iterable[int],
        score_fn: ScoreFn = score_transaction,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.r = client
        self.keys = keys
        self.session_factory = session_factory
        self.ctx = ctx
        self.cfg = cfg
        self.worker_id = worker_id
        self.shards = sorted(set(shards))
        self.score_fn = score_fn
        self.sleep = sleep
        self.owned: set[int] = set()
        self.stats = WorkerStats()
        self._renew = self.r.register_script(RENEW_LEASE)
        self._last_renew_all = 0.0
        self._release = self.r.register_script(RELEASE_LEASE)

    # ---------------------------------------------------------------- leases and groups

    def ensure_groups(self) -> None:
        for n in self.shards:
            try:
                self.r.xgroup_create(self.keys.shard(n), GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    def refresh_leases(self) -> set[int]:
        self._last_renew_all = time.monotonic()
        for n in self.shards:
            key = self.keys.lease(n)
            if n in self.owned:
                if not self._renew(keys=[key], args=[self.worker_id, self.cfg.lease_ms]):
                    self.owned.discard(n)
                    log.warning("lease_lost", shard=n, worker=self.worker_id)
            elif self.r.set(key, self.worker_id, nx=True, px=self.cfg.lease_ms):
                self.owned.add(n)
                self._claim_pending(n)
                log.info("lease_acquired", shard=n, worker=self.worker_id)
        return self.owned

    def _renew_one(self, n: int) -> bool:
        return bool(
            self._renew(keys=[self.keys.lease(n)], args=[self.worker_id, self.cfg.lease_ms])
        )

    def _keep_lease(self, n: int) -> bool:
        """Verify ownership of shard ``n`` *and* extend its lease (atomic compare-and-PEXPIRE),
        and every third of a lease period also extend all other owned shards, so a long run over
        one shard's backlog never lets the other leases lapse and silently hand shards over."""
        now = time.monotonic()
        if now - self._last_renew_all >= self.cfg.lease_ms / 3000:
            for m in sorted(self.owned - {n}):
                if not self._renew_one(m):
                    self.owned.discard(m)
                    log.warning("lease_lost", shard=m, worker=self.worker_id)
                    WORKER_MESSAGES.labels("lease_lost").inc()
            self._last_renew_all = now
        if self._renew_one(n):
            return True
        self.owned.discard(n)
        log.warning("lease_lost", shard=n, worker=self.worker_id)
        WORKER_MESSAGES.labels("lease_lost").inc()
        return False

    def release_leases(self) -> None:
        for n in list(self.owned):
            self._release(keys=[self.keys.lease(n)], args=[self.worker_id])
        self.owned.clear()

    def _claim_pending(self, n: int) -> None:
        """Take over every pending message of the shard (left by a previous owner)."""
        start = "0-0"
        while True:
            res = self.r.xautoclaim(
                self.keys.shard(n),
                GROUP,
                self.worker_id,
                min_idle_time=0,
                start_id=start,
                count=1000,
            )
            start = res[0]
            if start in ("0-0", b"0-0"):
                break

    # ---------------------------------------------------------------- main loop

    def run_once(self, block_ms: int | None = None) -> int:
        """Process available messages on owned shards. Returns how many were handled."""
        self.refresh_leases()
        if not self.owned:
            return 0
        handled = 0
        # Pending first (claimed or left over after a crash), so stream order is preserved.
        for since in ("0", ">"):
            while self.owned:
                streams: dict[Any, Any] = {self.keys.shard(n): since for n in sorted(self.owned)}
                block = None if since == "0" or handled else (block_ms or self.cfg.block_ms)
                resp = cast(
                    "list[tuple[str, list[tuple[str, dict[str, str] | None]]]] | None",
                    self.r.xreadgroup(
                        GROUP, self.worker_id, streams, count=self.cfg.read_count, block=block
                    ),
                )
                batch = 0
                for stream_key, entries in resp or []:
                    n = self.keys.shard_index(stream_key)
                    for mid, fields in entries:
                        if not self._keep_lease(n):
                            break  # leave the rest pending for the new owner
                        self._handle(n, mid, fields or {})
                        batch += 1
                handled += batch
                if batch == 0 or since == ">":
                    break
        return handled

    def run_forever(self, stop: threading.Event) -> None:
        self.ensure_groups()
        try:
            while not stop.is_set():
                if not self.run_once() and not self.owned:
                    stop.wait(self.cfg.block_ms / 1000)
        finally:
            self.release_leases()

    # ---------------------------------------------------------------- per message

    def _handle(self, n: int, mid: str, fields: dict[str, str]) -> None:
        attempts_key = self.keys.attempts(n)
        prior = int(self.r.hget(attempts_key, mid) or 0)
        if prior >= self.cfg.max_attempts:
            try:  # best effort, only for investigation identifiers in the dead letter
                known: Envelope | None = parse_envelope(fields)
            except MalformedMessage:
                known = None
            self._dead_letter(n, mid, fields, known, "attempts_exhausted", prior)
            return
        try:
            env = parse_envelope(fields)
        except MalformedMessage:
            self._dead_letter(n, mid, fields, None, "malformed_message", prior + 1)
            return
        try:
            req = env.request()
        except InvalidPayload as exc:
            self._dead_letter(n, mid, fields, env, "invalid_payload", prior + 1, str(exc))
            return

        attempt = prior
        while True:
            attempt = int(self.r.hincrby(attempts_key, mid, 1))
            try:
                self._attempt(n, mid, env, req.event_time, req)
                return
            except APIError as exc:
                if exc.status_code == 409:
                    self._dead_letter(n, mid, fields, env, exc.code, attempt)
                    return
                reason = exc.code
            except _Transient as exc:
                if exc.reason == "late_beyond_tolerance":
                    self._dead_letter(n, mid, fields, env, exc.reason, attempt)
                    return
                reason = exc.reason
            except SQLAlchemyError as exc:
                DB_ERRORS.labels("worker").inc()
                reason = f"database_error:{type(exc).__name__}"
            except redis.RedisError as exc:
                REDIS_ERRORS.labels("worker", "worker").inc()
                reason = f"redis_error:{type(exc).__name__}"
            except Exception as exc:
                log.exception("stream_unexpected_error", error_type=type(exc).__name__)
                reason = f"unexpected_error:{type(exc).__name__}"
            self.stats.add("retried")
            log.warning("stream_transient_failure", shard=n, attempt=attempt, reason=reason)
            if attempt >= self.cfg.max_attempts:
                self._dead_letter(n, mid, fields, env, "retries_exhausted", attempt, reason)
                return
            self.sleep(min(self.cfg.backoff_base_s * 2 ** (attempt - 1), self.cfg.backoff_max_s))
            if not self._keep_lease(n):
                return  # stays pending; the next owner continues counting attempts

    def _attempt(self, n: int, mid: str, env: Envelope, event_time: datetime, req: Any) -> None:
        with self.session_factory() as session:
            latest = session.scalar(
                select(func.max(Transaction.event_time)).where(
                    Transaction.organization_id == env.organization_id,
                    Transaction.customer_id == req.customer_id,
                    Transaction.external_id != req.transaction_id,
                )
            )
            late = latest is not None and latest > event_time
            if (
                latest is not None
                and late
                and not env.allow_late
                and latest - event_time > timedelta(seconds=self.cfg.late_tolerance_s)
            ):
                raise _Transient("late_beyond_tolerance")
            path_token = SCORING_PATH.set("stream")
            try:
                response = self.score_fn(
                    session, self.ctx, env.organization_id, env.actor_id, req, env.correlation_id
                )
            finally:
                SCORING_PATH.reset(path_token)
        payload = feed_payload(response, "stream", event_time, env.ingested_at, late)
        pipe = self.r.pipeline(transaction=True)
        pipe.xadd(
            self.keys.feed(env.organization_id),
            {"data": json.dumps(payload, separators=(",", ":"))},
            maxlen=self.cfg.feed_max_len,
            approximate=True,
        )
        self._finish(pipe, n, mid)
        self.ack(pipe)
        outcome = "replayed" if response.idempotent_replay else response.status
        self.stats.add(outcome)
        if late:
            self.stats.add("late_arrival")

    def ack(self, pipe: Any) -> None:
        """Execute the post-commit MULTI (feed + ack). Separate so tests can simulate a crash."""
        pipe.execute()

    def _finish(self, pipe: Any, n: int, mid: str) -> None:
        pipe.xack(self.keys.shard(n), GROUP, mid)
        pipe.xdel(self.keys.shard(n), mid)
        pipe.hdel(self.keys.attempts(n), mid)

    def _dead_letter(
        self,
        n: int,
        mid: str,
        fields: dict[str, str],
        env: Envelope | None,
        reason: str,
        attempts: int,
        detail: str | None = None,
    ) -> None:
        tid = None
        if env is not None:
            raw_tid = env.payload.get("transaction_id")
            tid = raw_tid if isinstance(raw_tid, str) else None
        entry: dict[Any, Any] = {
            "reason": reason,
            "detail": (DLQ_REASONS.get(reason, "") + (f" [{detail}]" if detail else ""))[:500],
            "source_shard": str(n),
            "source_id": mid,
            "org": str(env.organization_id) if env else "",
            "transaction_id": tid or "",
            "attempts": str(attempts),
            "failed_at": datetime.now(UTC).isoformat(),
            "worker": self.worker_id,
            # Original envelope for investigation and replay (internal ops stream only).
            "envelope": json.dumps(fields, separators=(",", ":"))[:65536],
        }
        pipe = self.r.pipeline(transaction=True)
        pipe.xadd(self.keys.dlq, entry)
        self._finish(pipe, n, mid)
        pipe.execute()
        self.stats.add("dead_lettered")
        log.warning("stream_dead_letter", shard=n, reason=reason, attempts=attempts)


def parse_shards(spec: str, total: int) -> list[int]:
    if spec == "all":
        return list(range(total))
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a), int(b or a) + 1))
    if not out or max(out) >= total or min(out) < 0:
        raise ValueError(f"shards must be within 0..{total - 1}")
    return sorted(out)


def build_worker(
    settings: Settings, worker_id: str, shards: list[int], cfg: WorkerConfig
) -> StreamWorker:
    if settings.redis_url is None:
        raise SystemExit("FRAUD_REDIS_URL is not set")
    session_factory = make_session_factory(make_engine(settings))
    ctx = build_scoring_context(settings, session_factory)
    client = redis.Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    keys = StreamKeys(settings.stream_prefix, settings.stream_shards)
    return StreamWorker(client, keys, session_factory, ctx, cfg, worker_id, shards)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fraud_api.stream.worker")
    p.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    p.add_argument("--shards", default="all", help="'all' or e.g. '0-3,6'")
    p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--lease-ms", type=int, default=15_000)
    p.add_argument("--block-ms", type=int, default=1_000)
    p.add_argument("--stats-every-s", type=float, default=10.0)
    p.add_argument(
        "--metrics-port",
        type=int,
        default=0,
        help="serve Prometheus metrics on 127.0.0.1:PORT (0 = off); contains no event data",
    )
    args = p.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    cfg = WorkerConfig(
        max_attempts=args.max_attempts,
        lease_ms=args.lease_ms,
        block_ms=args.block_ms,
        late_tolerance_s=settings.stream_late_tolerance_seconds,
        feed_max_len=settings.feed_max_len,
    )
    worker = build_worker(
        settings, args.worker_id, parse_shards(args.shards, settings.stream_shards), cfg
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def report() -> None:
        while not stop.wait(args.stats_every_s):
            log.info(
                "stream_worker_stats",
                worker=args.worker_id,
                owned=sorted(worker.owned),
                **dict(worker.stats.outcomes),
            )

    threading.Thread(target=report, daemon=True).start()
    if args.metrics_port:
        from prometheus_client import start_http_server

        if worker.ctx.model is not None:
            MODEL_INFO.labels(
                worker.ctx.model.name, worker.ctx.model.version, worker.ctx.policy.version
            ).set(1)
        start_http_server(args.metrics_port, addr="127.0.0.1", registry=REGISTRY)
        log.info("worker_metrics_listening", port=args.metrics_port)
    log.info("stream_worker_started", worker=args.worker_id, shards=worker.shards)
    worker.run_forever(stop)
    log.info("stream_worker_stopped", worker=args.worker_id, **dict(worker.stats.outcomes))


if __name__ == "__main__":
    main()
