"""Phase 9: /metrics access and content, stream gauges, concurrent idempotency, stream recovery
under load, and proof that the load tool cannot change other tenants' data or model state."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import re
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from fraud_api.core.security import Role
from fraud_api.db.models import RiskAssessment, Transaction
from fraud_api.stream.ops import dlq_list, stream_status
from tests.fixtures import synthetic_events
from tests.integration.conftest import ROOT, AppFactory, Tenant, create_tenant
from tests.integration.test_monitoring import items
from tests.integration.test_scoring_api import payload, score
from tests.integration.test_streaming import (  # noqa: F401 - pytest fixtures
    Crash,
    StreamEnv,
    ev,
    redis_url,
    rows,
    stream_factory,
)

pytestmark = pytest.mark.integration
TOKEN = "metrics-token-" + "x" * 30


def scrape(client: TestClient, token: str | None = TOKEN) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/metrics", headers=headers)


def sample(body: str, name: str, **labels: str) -> float:
    total = 0.0
    for line in body.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            total += float(line.rsplit(" ", 1)[1])
    return total


def test_metrics_endpoint_access_control(app_factory: AppFactory) -> None:
    closed = app_factory()
    assert scrape(closed).status_code == 404  # no token configured: not exposed
    open_ = app_factory(metrics_token=TOKEN)
    assert scrape(open_, None).status_code == 401
    assert scrape(open_, "wrong-token-" + "y" * 30).status_code == 401
    r = scrape(open_)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert open_.get("/health/live").status_code == 200  # health stays separate and public
    assert "/metrics" not in open_.get("/openapi.json").json()["paths"]
    with pytest.raises(ValueError, match="24"):
        app_factory(metrics_token="short")


def test_metrics_reflect_scoring_without_identifiers(
    app_factory: AppFactory, engine: Engine
) -> None:
    client = app_factory(metrics_token=TOKEN)
    tenant = create_tenant(engine, client, "acme")
    before = scrape(client).text
    base = sample(before, "fraud_scoring_decisions_total", path="single", outcome="scored")
    batch_before = sample(before, "fraud_batch_items_total", status="scored")
    r = score(client, tenant, payload("txn_secret_123"))
    assert r.status_code == 200
    score(client, tenant, payload("txn_secret_123"))  # idempotent replay
    client.post(
        "/api/v1/fraud/batch",
        headers=tenant.headers(),
        json={"items": [payload("b1", event_time="2020-03-15T15:00:00Z")]},
    )
    client.get(
        f"/api/v1/fraud/predictions/{r.json()['assessment_id']}",
        headers=tenant.headers(Role.VIEWER),
    )
    body = scrape(client).text
    assert (
        sample(body, "fraud_scoring_decisions_total", path="single", outcome="scored") == base + 1
    )
    assert sample(body, "fraud_scoring_decisions_total", path="single", outcome="replayed") >= 1
    assert sample(body, "fraud_batch_items_total", status="scored") == batch_before + 1
    assert sample(body, "fraud_scoring_duration_seconds_count", path="batch", stage="total") >= 1
    assert (
        sample(
            body,
            "fraud_http_requests_total",
            method="POST",
            route="/api/v1/fraud/score",
            status="200",
        )
        >= 2
    )
    assert 'route="/api/v1/fraud/predictions/{assessment_id}"' in body
    assert 'fraud_model_info{model="test-model"' in body
    for secret in (
        "txn_secret_123",
        str(tenant.org_id),
        "cus_00000000000000aa",
        r.json()["assessment_id"],
        tenant.tokens[Role.SERVICE][:20],
    ):
        assert secret not in body


def test_database_overload_is_503_with_retry_after_and_counted(
    app_factory: AppFactory, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import exc as sa_exc
    from sqlalchemy.orm import Session

    client = app_factory(metrics_token=TOKEN)
    tenant = create_tenant(engine, client, "acme")
    before = sample(scrape(client).text, "fraud_db_errors_total", component="api")

    def exhausted(self: Any, *a: Any, **k: Any) -> Any:
        raise sa_exc.TimeoutError("QueuePool limit reached")

    monkeypatch.setattr(Session, "execute", exhausted)
    r = score(client, tenant, payload("t_overload"))
    monkeypatch.undo()
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"
    assert r.headers["Retry-After"] == "2"
    assert sample(scrape(client).text, "fraud_db_errors_total", component="api") >= before + 1
    assert rows(engine, Transaction) == 0  # nothing persisted; the retry succeeds
    assert score(client, tenant, payload("t_overload")).status_code == 200


def test_stream_gauges_and_worker_metrics(stream_factory: Callable[..., StreamEnv]) -> None:  # noqa: F811
    env = stream_factory(metrics_token=TOKEN)
    t = env.tenant("acme")
    assert (
        env.ingest(t, [ev(f"m{i}", i, customer=f"cus_{i:016x}") for i in range(6)]).status_code
        == 202
    )
    body = scrape(env.client).text
    assert sample(body, "fraud_stream_up") == 1
    assert sample(body, "fraud_stream_backlog") == 6
    lags = [
        float(x)
        for x in re.findall(r'fraud_stream_consumer_lag_seconds\{shard="\d+"\} (\S+)', body)
    ]
    assert len(lags) == env.keys.shards and max(lags) >= 0
    assert sample(body, "fraud_ingest_events_total", result="accepted") >= 6
    w = env.worker()
    env.drain(w)
    body = scrape(env.client).text
    assert sample(body, "fraud_stream_backlog") == 0 and sample(body, "fraud_stream_pending") == 0
    assert sample(body, "fraud_stream_worker_messages_total", outcome="scored") >= 6
    assert (
        sample(body, "fraud_scoring_duration_seconds_count", path="stream", stage="features") >= 6
    )
    assert str(t.org_id) not in body


def test_concurrent_duplicate_batches_have_single_effects(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    events = synthetic_events(n=60, n_customers=6, seed=3)
    body = items(events)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(
                    "/api/v1/fraud/batch", json={"items": body}, headers=tenant.headers()
                ),
                range(10),
            )
        )
    assert all(r.status_code == 200 for r in responses), [r.text[:200] for r in responses]
    assert rows(engine, Transaction) == 60 and rows(engine, RiskAssessment) == 60
    ids = {
        it["transaction_id"]: it["result"]["assessment_id"]
        for r in responses
        for it in r.json()["items"]
        if it["result"]
    }
    assert len(ids) == 60  # every response names the same single assessment per transaction
    replays = sum(
        it["result"]["idempotent_replay"]
        for r in responses
        for it in r.json()["items"]
        if it["result"]
    )
    assert replays >= 60 * 9 - 60  # most concurrent copies were idempotent replays


def test_stream_recovers_from_a_worker_crash_under_load(
    stream_factory: Callable[..., StreamEnv],  # noqa: F811
) -> None:
    env = stream_factory()
    t = env.tenant("acme")
    events = [ev(f"l{i}", i / 10, customer=f"cus_{i % 40:016x}") for i in range(400)]
    for k in range(0, 400, 100):
        assert env.ingest(t, events[k : k + 100]).status_code == 202
    a = env.worker("a", shards=[0, 1], lease_ms=300)
    b = env.worker("b", shards=[2, 3], lease_ms=300)
    acked = {"n": 0}
    original_ack = a.ack

    def crash_after_50(pipe: Any) -> None:
        acked["n"] += 1
        if acked["n"] > 50:
            raise Crash()
        original_ack(pipe)

    a.ack = crash_after_50  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def run(w: Any) -> None:
        try:
            for _ in range(400):
                if w.run_once(block_ms=5) == 0 and env.backlog() == 0:
                    return
        except Crash as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(w,)) for w in (a, b)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors and acked["n"] == 51  # worker a died mid-run with a message committed, unacked
    import time

    time.sleep(0.4)  # a's leases expire
    c = env.worker("c", lease_ms=300)
    env.drain(c, b)
    assert rows(env.engine, Transaction) == 400 and rows(env.engine, RiskAssessment) == 400
    st = stream_status(env.r, env.keys)
    assert (st["backlog_total"], st["pending_total"], st["dead_letters"]) == (0, 0, 0)
    assert dlq_list(env.r, env.keys) == []
    assert c.stats.outcomes["replayed"] >= 1  # the crashed message was replayed, not duplicated


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("load_test", ROOT / "scripts" / "load_test.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_test"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def outside(engine: Engine, org_id: Any) -> str:
    """Everything the load tool must not change: other tenants' assessments, cases, labels and
    the model registry."""
    from sqlalchemy import text

    with engine.connect() as c:
        parts = [
            c.execute(
                text(
                    "SELECT string_agg(id::text||risk_score::text||decision||policy_decision"
                    "||rules_status, ',' ORDER BY id) FROM risk_assessments"
                    " WHERE organization_id <> :o"
                ),
                {"o": org_id},
            ).scalar(),
            c.execute(
                text("SELECT count(*)::text FROM transactions WHERE organization_id <> :o"),
                {"o": org_id},
            ).scalar(),
            c.execute(text("SELECT count(*)::text FROM cases")).scalar(),
            c.execute(text("SELECT count(*)::text FROM fraud_labels")).scalar(),
            c.execute(
                text(
                    "SELECT string_agg(name||version||status||artifact_sha256, ',' ORDER BY name,"
                    " version) FROM model_versions"
                )
            ).scalar(),
        ]
    return "|".join(str(p) for p in parts)


def test_load_tool_only_adds_its_own_tenant_data(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    lt = _load_tool()
    for i in range(3):
        a = score(client, tenant, payload(f"keep{i}", event_time=f"2020-03-15T1{i}:00:00Z"))
    client.post(
        "/api/v1/cases",
        json={"assessment_id": a.json()["assessment_id"]},
        headers=tenant.headers(Role.ANALYST),
    )
    load_tenant = create_tenant(engine, client, "loadtest")
    before = outside(engine, load_tenant.org_id)
    events = items(synthetic_events(n=80, n_customers=8, seed=9))
    for e in events:
        e["transaction_id"] = "ltunit_" + e["transaction_id"]
        e["source"] = "simulated"
    sendable = lt.with_duplicates(events, 0.1, seed=1)
    args = argparse.Namespace(mode="burst", concurrency=8, rate=100.0, batch_size=20)

    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            tok = load_tenant.tokens[Role.SERVICE]
            single = await lt.scenario_single(http, tok, sendable[:50], args)
            batch = await lt.scenario_batch(http, tok, sendable[50:], args)
            return single, batch

    single, batch = asyncio.run(go())
    assert single["ok"] == 50 and single["latency_ms"]["n"] == 50
    assert sum(batch["item_statuses"].values()) == len(sendable) - 50
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        load_ids = s.scalars(
            select(Transaction.external_id).where(Transaction.organization_id == load_tenant.org_id)
        ).all()
    # one transaction per unique id despite concurrent duplicates
    assert len(load_ids) == len(set(load_ids)) == len({e["transaction_id"] for e in sendable})
    assert outside(engine, load_tenant.org_id) == before
