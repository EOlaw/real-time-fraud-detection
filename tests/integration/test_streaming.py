"""Phase 5: Redis Streams ingestion, workers, retries, DLQ, SSE (PostgreSQL + Redis).

Requires ``docker compose up -d postgres redis``. Redis tests use DB 15 and a unique key prefix
per test, so they never touch development data in DB 0.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from fraud_api.core.config import FailureMode, Settings
from fraud_api.core.security import Role
from fraud_api.db.models import RiskAssessment, Transaction
from fraud_api.db.session import make_engine, make_session_factory
from fraud_api.main import create_app
from fraud_api.scoring.context import build_scoring_context
from fraud_api.scoring.service import score_transaction
from fraud_api.stream.keys import GROUP, StreamKeys
from fraud_api.stream.messages import make_envelope
from fraud_api.stream.ops import dlq_list, dlq_replay, stream_status
from fraud_api.stream.worker import StreamWorker, WorkerConfig
from tests.fixtures import synthetic_events
from tests.integration.conftest import ROOT, Tenant, create_tenant

pytestmark = pytest.mark.integration

T0 = datetime(2020, 3, 15, 14, 30, tzinfo=UTC)


def _redis_url() -> str | None:
    if url := os.environ.get("FRAUD_TEST_REDIS_URL"):
        return url
    base = os.environ.get("FRAUD_REDIS_URL")
    if not base and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("FRAUD_REDIS_URL="):
                base = line.split("=", 1)[1].strip()
    return base.rsplit("/", 1)[0] + "/15" if base else None


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = _redis_url()
    if url is None:
        pytest.skip("No Redis configured (set FRAUD_TEST_REDIS_URL or FRAUD_REDIS_URL)")
    try:
        redis.Redis.from_url(url).ping()
    except redis.RedisError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis not reachable: {type(exc).__name__}")
    return url


class Crash(BaseException):
    """Simulates a process dying: not an Exception, so worker error handling cannot catch it."""


@dataclass
class StreamEnv:
    settings: Settings
    client: TestClient
    r: redis.Redis
    keys: StreamKeys
    engine: Engine
    _ctx: Any = None
    workers: list[StreamWorker] = field(default_factory=list)

    def tenant(self, slug: str) -> Tenant:
        return create_tenant(self.engine, self.client, slug)

    def worker(
        self,
        wid: str = "w1",
        shards: list[int] | None = None,
        score_fn: Callable[..., Any] = score_transaction,
        **cfg: Any,
    ) -> StreamWorker:
        factory = make_session_factory(make_engine(self.settings))
        if self._ctx is None:
            self._ctx = build_scoring_context(self.settings, factory)
        conf = WorkerConfig(
            **{
                "block_ms": 10,
                "lease_ms": 2_000,
                "backoff_base_s": 0.001,
                "max_attempts": 3,
                "late_tolerance_s": self.settings.stream_late_tolerance_seconds,
                **cfg,
            }
        )
        w = StreamWorker(
            self.r,
            self.keys,
            factory,
            self._ctx,
            conf,
            wid,
            shards if shards is not None else range(self.keys.shards),
            score_fn=score_fn,
            sleep=lambda s: None,
        )
        w.ensure_groups()
        self.workers.append(w)
        return w

    def backlog(self) -> int:
        return sum(int(self.r.xlen(self.keys.shard(n))) for n in range(self.keys.shards))

    def drain(self, *workers: StreamWorker, rounds: int = 200) -> None:
        for _ in range(rounds):
            handled = sum(w.run_once(block_ms=5) for w in workers)
            if handled == 0 and self.backlog() == 0:
                return
        raise AssertionError(f"streams not drained; backlog {self.backlog()}")

    def ingest(self, tenant: Tenant, items: list[Any], role: Role = Role.SERVICE) -> Any:
        return self.client.post(
            "/api/v1/events", json={"items": items}, headers=tenant.headers(role)
        )

    def sse(self, tenant: Tenant, **params: Any) -> list[dict[str, Any]]:
        headers = tenant.headers(Role.VIEWER)
        if "last_event_id" in params:
            headers["Last-Event-ID"] = params.pop("last_event_id")
        events, current = [], {}
        with self.client.stream(
            "GET", "/api/v1/stream/assessments", params=params, headers=headers
        ) as resp:
            assert resp.status_code == 200, resp.read()
            for line in resp.iter_lines():
                if line == "":
                    if current:
                        events.append(current)
                    current = {}
                elif line.startswith(":"):
                    current = {"event": "heartbeat"}
                else:
                    k, _, v = line.partition(": ")
                    current[k] = v
        out = []
        for e in events:
            if e.get("event") in ("assessment", "reset"):
                out.append({**e, "data": json.loads(e["data"])})
        return out


@pytest.fixture
def stream_factory(
    db_url: str, engine: Engine, redis_url: str, trained_artifact: Path, policy_path: Path
) -> Iterator[Callable[..., StreamEnv]]:
    envs: list[tuple[StreamEnv, str]] = []

    def factory(**overrides: Any) -> StreamEnv:
        prefix = f"t{uuid.uuid4().hex[:10]}"
        settings = Settings(
            database_url=db_url,  # type: ignore[arg-type]
            jwt_secret=secrets.token_urlsafe(48),  # type: ignore[arg-type]
            model_dir=overrides.pop("model_dir", trained_artifact),
            policy_path=policy_path,
            redis_url=redis_url,  # type: ignore[arg-type]
            stream_prefix=prefix,
            stream_shards=overrides.pop("stream_shards", 4),
            **overrides,
        )
        client = TestClient(create_app(settings))
        client.__enter__()
        env = StreamEnv(
            settings,
            client,
            redis.Redis.from_url(redis_url, decode_responses=True),
            StreamKeys(prefix, settings.stream_shards),
            engine,
        )
        envs.append((env, prefix))
        return env

    yield factory
    for env, prefix in envs:
        env.client.__exit__(None, None, None)
        for key in env.r.scan_iter(f"{prefix}:*"):
            env.r.delete(key)


@pytest.fixture
def env(stream_factory: Callable[..., StreamEnv]) -> StreamEnv:
    return stream_factory()


def ev(
    txn: str, minutes: float = 0, customer: str = "cus_00000000000000aa", **overrides: Any
) -> dict[str, Any]:
    body = {
        "transaction_id": txn,
        "customer_id": customer,
        "merchant_id": "Acme",
        "amount": "125.40",
        "currency": "USD",
        "category": "shopping_net",
        "event_time": (T0 + timedelta(minutes=minutes)).isoformat(),
        "source": "replay",
    }
    body.update(overrides)
    return body


def rows(engine: Engine, model: Any) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def features_by_txn(engine: Engine, org: uuid.UUID) -> dict[str, Any]:
    with Session(engine) as s:
        out = s.execute(
            select(
                Transaction.external_id,
                RiskAssessment.features,
                RiskAssessment.risk_score,
                RiskAssessment.decision,
            )
            .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
            .where(Transaction.organization_id == org)
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in out}


# ---------------------------------------------------------------- ingest


def test_ingest_validates_labels_and_shards_with_tenant_from_token(env: StreamEnv) -> None:
    t = env.tenant("acme")
    items = [
        ev("a1", customer="cus_00000000000000a1"),
        ev("bad_time", event_time="2020-03-15T14:30:00"),
        ev("a2", customer="cus_00000000000000a2", source="simulated"),
        {**ev("spoof"), "organization_id": str(uuid.uuid4())},
    ]
    r = env.ingest(t, items)
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (2, 2)
    assert [i["status"] for i in body["items"]] == ["queued", "invalid", "queued", "invalid"]
    for it, customer in (
        (body["items"][0], "cus_00000000000000a1"),
        (body["items"][2], "cus_00000000000000a2"),
    ):
        assert it["shard"] == env.keys.shard_for(t.org_id, customer)
        ((_mid, fields),) = env.r.xrange(
            env.keys.shard(it["shard"]), it["message_id"], it["message_id"]
        )
        assert fields["org"] == str(t.org_id)  # tenant from the token, never the body
        assert fields["ingested_at"]
    labels = {
        json.loads(f["payload"])["source"]
        for n in range(env.keys.shards)
        for _, f in env.r.xrange(env.keys.shard(n))
    }
    assert labels == {"replay", "simulated"}
    assert env.ingest(t, [ev("x")], role=Role.VIEWER).status_code == 403
    assert env.client.post("/api/v1/events", json={"items": [ev("x")]}).status_code == 401


def test_streaming_endpoints_unavailable_without_redis(client: TestClient, tenant: Tenant) -> None:
    r = client.post("/api/v1/events", json={"items": [ev("x")]}, headers=tenant.headers())
    assert r.status_code == 503 and r.json()["error"]["code"] == "stream_unavailable"
    r = client.get("/api/v1/stream/assessments", headers=tenant.headers(Role.VIEWER))
    assert r.status_code == 503


def test_backpressure_rejects_whole_request(stream_factory: Callable[..., StreamEnv]) -> None:
    env = stream_factory(stream_max_backlog=2)
    t = env.tenant("acme")
    assert env.ingest(t, [ev("b1", 1), ev("b2", 2)]).status_code == 202
    r = env.ingest(t, [ev("b3", 3)])
    assert r.status_code == 503 and r.json()["error"]["code"] == "ingest_backpressure"
    assert r.headers["retry-after"] == "5"
    assert env.backlog() == 2  # nothing partially queued


# ---------------------------------------------------------------- end-to-end and parity


def test_end_to_end_scoring_feed_and_clean_streams(env: StreamEnv) -> None:
    t = env.tenant("acme")
    items = [ev(f"e{i}", i, customer=f"cus_{i % 3:016x}") for i in range(12)]
    assert env.ingest(t, items).json()["accepted"] == 12
    w = env.worker()
    env.drain(w)
    assert rows(env.engine, Transaction) == 12 and rows(env.engine, RiskAssessment) == 12
    status = stream_status(env.r, env.keys)
    assert (status["backlog_total"], status["pending_total"], status["dead_letters"]) == (0, 0, 0)
    assert all(not env.r.exists(env.keys.attempts(n)) for n in range(env.keys.shards))
    feed = env.sse(t, cursor="0-0", limit=12)
    assert sorted(e["data"]["transaction_id"] for e in feed) == sorted(
        i["transaction_id"] for i in items
    )
    for e in feed:
        assert e["data"]["path"] == "stream" and e["data"]["source"] == "replay"
        assert e["data"]["late_arrival"] is False and e["data"]["ingested_at"]
        blob = json.dumps(e["data"])
        assert "customer" not in blob and "amount" not in blob and "merchant" not in blob
    assert w.stats.outcomes["scored"] == 12


def test_stream_scoring_matches_synchronous_scoring(env: StreamEnv) -> None:
    a, b = env.tenant("streamorg"), env.tenant("syncorg")
    data = synthetic_events(n=150, n_customers=8, seed=5)
    items = [
        ev(
            r.event_id,
            customer=r.customer_id,
            amount=f"{r.amount:.2f}",
            category=r.category,
            event_time=r.event_time.isoformat(),
        )
        for r in data.itertuples()
    ]
    items.sort(key=lambda e: (e["event_time"], e["transaction_id"]))
    for k in range(0, len(items), 40):  # ingest in time order, several requests
        assert env.ingest(a, items[k : k + 40]).status_code == 202
    env.drain(env.worker("w1", shards=[0, 1]), env.worker("w2", shards=[2, 3]))
    sync = env.client.post("/api/v1/fraud/batch", json={"items": items}, headers=b.headers())
    assert sync.json()["summary"]["scored"] == len(items)
    sa, sb = features_by_txn(env.engine, a.org_id), features_by_txn(env.engine, b.org_id)
    assert set(sa) == set(sb) and len(sa) == len(items)
    assert all(sa[k] == sb[k] for k in sa)  # features, score and decision identical


def test_same_customer_order_is_preserved(env: StreamEnv) -> None:
    t = env.tenant("acme")
    for k in range(3):  # three separate ingest requests, interleaving two customers
        env.ingest(
            t,
            [
                ev(f"x{k}", k, customer="cus_000000000000000x"),
                ev(f"y{k}", k, customer="cus_000000000000000y"),
            ],
        )
    env.drain(env.worker("w1", shards=[0, 1]), env.worker("w2", shards=[2, 3]))
    f = features_by_txn(env.engine, t.org_id)
    assert [f[f"x{k}"][0]["cust_txn_count_1h"] for k in range(3)] == [0, 1, 2]
    assert [f[f"y{k}"][0]["cust_txn_count_1h"] for k in range(3)] == [0, 1, 2]
    feed = [e["data"]["transaction_id"] for e in env.sse(t, cursor="0-0", limit=6)]
    assert [x for x in feed if x.startswith("x")] == ["x0", "x1", "x2"]


# ---------------------------------------------------------------- late arrivals


def test_late_arrival_within_tolerance_is_scored_and_flagged(env: StreamEnv) -> None:
    t = env.tenant("acme")
    w = env.worker()
    env.ingest(t, [ev("later", 60)])
    env.drain(w)
    env.ingest(t, [ev("earlier", 0)])
    env.drain(w)
    f = features_by_txn(env.engine, t.org_id)
    assert f["earlier"][0]["cust_txn_count_1h"] == 0  # the later event is not history for it
    assert f["later"][0]["cust_txn_count_1h"] == 0  # not re-scored retroactively
    late = {
        e["data"]["transaction_id"]: e["data"]["late_arrival"]
        for e in env.sse(t, cursor="0-0", limit=2)
    }
    assert late == {"later": False, "earlier": True}


def test_late_beyond_tolerance_is_dead_lettered_then_replayable(
    stream_factory: Callable[..., StreamEnv],
) -> None:
    env = stream_factory(stream_late_tolerance_seconds=3600)
    t = env.tenant("acme")
    w = env.worker()
    env.ingest(t, [ev("recent", 60 * 48)])
    env.drain(w)
    env.ingest(t, [ev("stale", 0)])
    env.drain(w)
    (entry,) = dlq_list(env.r, env.keys)
    assert entry["reason"] == "late_beyond_tolerance" and entry["transaction_id"] == "stale"
    assert set(features_by_txn(env.engine, t.org_id)) == {"recent"}
    (res,) = dlq_replay(env.r, env.keys, [entry["dlq_id"]], allow_late=True)
    assert res["status"] == "requeued"
    env.drain(w)
    assert set(features_by_txn(env.engine, t.org_id)) == {"recent", "stale"}
    assert dlq_list(env.r, env.keys) == []


# ---------------------------------------------------------------- at-least-once delivery


def test_duplicate_delivery_is_idempotent(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev("dup", 1)])
    env.ingest(t, [ev("dup", 1)])  # redelivered by the producer
    w = env.worker()
    env.drain(w)
    assert rows(env.engine, Transaction) == 1 and rows(env.engine, RiskAssessment) == 1
    feed = env.sse(t, cursor="0-0", limit=2)
    assert len({e["data"]["assessment_id"] for e in feed}) == 1
    assert [e["data"]["idempotent_replay"] for e in feed] == [False, True]
    assert w.stats.outcomes["replayed"] == 1


def test_worker_crash_before_ack_is_recovered_without_duplicates(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev("c1", 1), ev("c2", 2)])
    a = env.worker("worker-a", lease_ms=200)

    def crash(_pipe: Any) -> None:
        raise Crash()

    a.ack = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        a.run_once(block_ms=5)
    # The assessment is committed, but the message is still pending for worker-a.
    assert rows(env.engine, RiskAssessment) == 1
    shard = env.keys.shard_for(t.org_id, "cus_00000000000000aa")
    assert env.r.xpending(env.keys.shard(shard), GROUP)["pending"] >= 1

    b = env.worker("worker-b", lease_ms=200)
    assert b.run_once(block_ms=5) == 0  # lease still held by the "dead" worker
    time.sleep(0.3)  # lease expires
    env.drain(b)
    assert rows(env.engine, Transaction) == 2 and rows(env.engine, RiskAssessment) == 2
    assert b.stats.outcomes["replayed"] == 1 and b.stats.outcomes["scored"] == 1
    assert stream_status(env.r, env.keys)["pending_total"] == 0
    assert len({e["data"]["assessment_id"] for e in env.sse(t, cursor="0-0", limit=2)}) == 2


def test_crash_loop_is_dead_lettered_after_max_attempts(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev("poison", 1)])
    calls = {"n": 0}

    def crashing_score(*_: Any, **__: Any) -> Any:
        calls["n"] += 1
        raise Crash()

    for k in range(3):  # three workers die in a row on the same message
        w = env.worker(f"crash-{k}", score_fn=crashing_score, lease_ms=50)
        with pytest.raises(Crash):
            w.run_once(block_ms=5)
        time.sleep(0.08)
    survivor = env.worker("survivor", lease_ms=50)
    env.drain(survivor)
    assert calls["n"] == 3  # never attempted a 4th time
    (entry,) = dlq_list(env.r, env.keys)
    assert (entry["reason"], entry["attempts"], entry["transaction_id"]) == (
        "attempts_exhausted",
        "3",
        "poison",
    )
    assert rows(env.engine, Transaction) == 0


# ---------------------------------------------------------------- retries and DLQ


def _flaky(failures: int) -> Callable[..., Any]:
    state = {"n": 0}

    def score(*args: Any, **kwargs: Any) -> Any:
        state["n"] += 1
        if state["n"] <= failures:
            raise OperationalError("SELECT 1", {}, Exception("connection lost to 10.0.0.5"))
        return score_transaction(*args, **kwargs)

    return score


def test_transient_failure_is_retried_then_scored(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev("tr", 1)])
    sleeps: list[float] = []
    w = env.worker(score_fn=_flaky(2), max_attempts=5, backoff_base_s=0.01)
    w.sleep = sleeps.append  # type: ignore[assignment]
    env.drain(w)
    assert w.stats.outcomes["retried"] == 2 and w.stats.outcomes["scored"] == 1
    assert sleeps == [0.01, 0.02]  # bounded exponential backoff
    assert dlq_list(env.r, env.keys) == []
    assert rows(env.engine, RiskAssessment) == 1


def test_retry_exhaustion_goes_to_dlq_and_replay_scores_it(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev("ex", 1)])
    w = env.worker(score_fn=_flaky(10_000), max_attempts=3)
    env.drain(w)
    (entry,) = dlq_list(env.r, env.keys)
    assert entry["reason"] == "retries_exhausted" and entry["attempts"] == "3"
    assert entry["org"] == str(t.org_id) and entry["transaction_id"] == "ex"
    assert "OperationalError" in entry["detail"] and "10.0.0.5" not in entry["detail"]
    assert rows(env.engine, RiskAssessment) == 0  # a failure never becomes an assessment
    assert stream_status(env.r, env.keys)["pending_total"] == 0

    dlq_replay(env.r, env.keys, None)
    w.release_leases()  # as a stopped worker does; otherwise its leases block the new one
    env.drain(env.worker("healthy"))
    assert rows(env.engine, RiskAssessment) == 1
    assert dlq_list(env.r, env.keys) == []


def test_poison_messages_are_dead_lettered_immediately(env: StreamEnv) -> None:
    t = env.tenant("acme")
    w = env.worker()
    env.r.xadd(env.keys.shard(0), {"garbage": "4111111111111111"})
    bad = make_envelope(
        t.org_id,
        t.org_id,
        "cid",
        __import__(
            "fraud_api.scoring.schemas", fromlist=["ScoreRequest"]
        ).ScoreRequest.model_validate(ev("inv")),
    ).fields()
    payload = json.loads(bad["payload"])
    payload["category"] = "crypto-secret-value"
    bad["payload"] = json.dumps(payload)
    env.r.xadd(env.keys.shard(1), bad)
    env.drain(w)
    reasons = {e["reason"]: e for e in dlq_list(env.r, env.keys)}
    assert set(reasons) == {"malformed_message", "invalid_payload"}
    assert all(e["attempts"] == "1" for e in reasons.values())
    assert "crypto-secret-value" not in reasons["invalid_payload"]["detail"]
    assert "category" in reasons["invalid_payload"]["detail"]
    assert (
        dlq_replay(env.r, env.keys, [reasons["malformed_message"]["dlq_id"]])[0]["status"]
        == "not_replayable"
    )


def test_conflicting_payload_is_dead_lettered_without_retries(env: StreamEnv) -> None:
    t = env.tenant("acme")
    assert (
        env.client.post("/api/v1/fraud/score", json=ev("k1", 1), headers=t.headers()).status_code
        == 200
    )
    env.ingest(t, [ev("k1", 1, amount="9.99")])
    w = env.worker()
    env.drain(w)
    (entry,) = dlq_list(env.r, env.keys)
    assert entry["reason"] == "idempotency_conflict" and entry["attempts"] == "1"
    assert w.stats.outcomes["retried"] == 0


def test_failures_never_become_allow(
    stream_factory: Callable[..., StreamEnv], tmp_path: Path
) -> None:
    review = stream_factory(model_dir=tmp_path / "missing")  # manual_review mode
    t = review.tenant("acme")
    review.ingest(t, [ev("m1", 1)])
    review.drain(review.worker())
    (e,) = review.sse(t, cursor="0-0", limit=1)
    assert (e["data"]["status"], e["data"]["decision"], e["data"]["risk_score"]) == (
        "degraded",
        "review",
        None,
    )

    rejecting = stream_factory(model_dir=tmp_path / "missing", failure_mode=FailureMode.REJECT)
    t2 = rejecting.tenant("globex")
    rejecting.ingest(t2, [ev("m2", 1)])
    rejecting.drain(rejecting.worker())
    (entry,) = dlq_list(rejecting.r, rejecting.keys)
    assert entry["reason"] == "retries_exhausted" and "scoring_unavailable" in entry["detail"]
    with Session(rejecting.engine) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(RiskAssessment)
                .where(RiskAssessment.organization_id == t2.org_id)
            )
            == 0
        )


# ---------------------------------------------------------------- leases


def test_one_worker_per_shard(env: StreamEnv) -> None:
    a, b = env.worker("a"), env.worker("b")
    assert a.refresh_leases() == set(range(env.keys.shards))
    assert b.refresh_leases() == set()
    a.release_leases()
    assert b.refresh_leases() == set(range(env.keys.shards))


# ---------------------------------------------------------------- tenant isolation and SSE


def test_feed_is_tenant_scoped(env: StreamEnv) -> None:
    a, b = env.tenant("acme"), env.tenant("globex")
    env.ingest(a, [ev("a_only", 1)])
    env.ingest(b, [ev("b_only", 2)])
    env.drain(env.worker())
    assert [e["data"]["transaction_id"] for e in env.sse(b, cursor="0-0", idle_timeout=0.3)] == [
        "b_only"
    ]
    assert [e["data"]["transaction_id"] for e in env.sse(a, cursor="0-0", idle_timeout=0.3)] == [
        "a_only"
    ]
    assert (
        env.client.get("/api/v1/stream/status", headers=a.headers(Role.ANALYST)).status_code == 403
    )
    assert (
        env.client.get("/api/v1/stream/status", headers=a.headers(Role.ADMIN)).json()[
            "backlog_total"
        ]
        == 0
    )


def test_sse_reconnect_cursor_and_retention_reset(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev(f"s{i}", i) for i in range(5)])
    env.drain(env.worker())
    first = env.sse(t, cursor="0-0", limit=2)
    assert [e["data"]["transaction_id"] for e in first] == ["s0", "s1"]
    rest = env.sse(t, last_event_id=first[-1]["id"], limit=3)
    assert [e["data"]["transaction_id"] for e in rest] == ["s2", "s3", "s4"]
    assert env.sse(t, last_event_id=rest[-1]["id"], idle_timeout=0.3) == []  # nothing missed
    assert env.sse(t, idle_timeout=0.3) == []  # no cursor: only new events

    r = env.client.get(
        "/api/v1/stream/assessments", params={"cursor": "bogus"}, headers=t.headers(Role.VIEWER)
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cursor"
    assert env.client.get("/api/v1/stream/assessments").status_code == 401

    env.r.xtrim(env.keys.feed(t.org_id), maxlen=2, approximate=False)  # drops s0..s2
    after_trim = env.sse(t, cursor=first[0]["id"], idle_timeout=0.5)
    assert after_trim[0]["event"] == "reset"
    assert after_trim[0]["data"]["reason"] == "cursor_expired"
    assert [e["data"]["transaction_id"] for e in after_trim[1:]] == ["s3", "s4"]
    from_start = env.sse(t, cursor="0-0", idle_timeout=0.3)  # history was trimmed: say so
    assert from_start[0]["event"] == "reset" and len(from_start) == 3


def test_synchronous_scoring_also_publishes_to_feed(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.client.post("/api/v1/fraud/score", json=ev("sync1", 1), headers=t.headers())
    env.client.post("/api/v1/fraud/batch", json={"items": [ev("batch1", 2)]}, headers=t.headers())
    feed = env.sse(t, cursor="0-0", limit=2)
    assert [(e["data"]["transaction_id"], e["data"]["path"]) for e in feed] == [
        ("sync1", "api"),
        ("batch1", "batch"),
    ]


def test_streaming_does_not_touch_other_redis_databases(env: StreamEnv) -> None:
    assert env.r.connection_pool.connection_kwargs.get("db") == 15
    with env.engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_idle_feed_survives_longer_than_redis_default_timeout(
    stream_factory: Callable[..., StreamEnv],
) -> None:
    """Regression: an SSE connection with no events must not hit the Redis read timeout."""
    env = stream_factory(sse_heartbeat_seconds=6.0)
    t = env.tenant("acme")
    started = time.monotonic()
    assert env.sse(t, idle_timeout=6.5) == []  # one 6 s blocking read > redis-py's 5 s default
    assert time.monotonic() - started >= 6.0


def test_cursor_still_retained_does_not_reset(env: StreamEnv) -> None:
    t = env.tenant("acme")
    env.ingest(t, [ev(f"k{i}", i) for i in range(4)])
    env.drain(env.worker())
    all_events = env.sse(t, cursor="0-0", limit=4)
    env.r.xtrim(env.keys.feed(t.org_id), maxlen=3, approximate=False)  # drops k0 only
    resumed = env.sse(t, cursor=all_events[1]["id"], idle_timeout=0.3)  # cursor k1 retained
    assert [e["event"] for e in resumed] == ["assessment", "assessment"]
    assert [e["data"]["transaction_id"] for e in resumed] == ["k2", "k3"]


def test_lease_is_renewed_per_message_during_long_runs(env: StreamEnv) -> None:
    """Regression (found in the real-data drill): draining a backlog that takes longer than the
    lease must not let the lease expire mid-run."""
    t = env.tenant("acme")
    env.ingest(t, [ev(f"l{i}", i, customer=f"cus_{i % 4:016x}") for i in range(40)])
    real = score_transaction

    def slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(0.02)  # 40 messages x 20 ms = 0.8 s, far beyond the 150 ms lease
        return real(*args, **kwargs)

    w = env.worker("slow", score_fn=slow, lease_ms=150, read_count=100)
    handled = w.run_once(block_ms=5)
    assert handled == 40  # one run_once drained everything without losing a lease
    assert w.owned == set(range(env.keys.shards))
    assert rows(env.engine, RiskAssessment) == 40
