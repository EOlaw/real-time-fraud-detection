"""Phase 7: API contract the command center depends on (list fields, source filter, SSE headers)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from fraud_api.core.security import Role
from tests.integration.conftest import Tenant
from tests.integration.test_rules import MID, live_rule
from tests.integration.test_scoring_api import payload, score
from tests.integration.test_streaming import (  # noqa: F401 - pytest fixtures
    StreamEnv,
    redis_url,
    stream_factory,
)


def test_sse_response_must_not_be_compressed_by_proxies(
    stream_factory: Callable[..., StreamEnv],  # noqa: F811 - imported fixture
) -> None:
    # The Next.js server gzips proxied responses unless told not to, which buffers SSE events
    # until a compression block fills (observed in the Phase 7 browser checks).
    env = stream_factory()
    t = env.tenant("acme")
    with env.client.stream(
        "GET",
        "/api/v1/stream/assessments",
        params={"limit": 1, "idle_timeout": 0.5},
        headers=t.headers(Role.VIEWER),
    ) as resp:
        assert resp.status_code == 200
        assert "no-transform" in resp.headers["cache-control"]
        assert resp.headers["content-type"].startswith("text/event-stream")


pytestmark = pytest.mark.integration


def test_list_rows_carry_policy_decision_and_rules_status(
    client: TestClient, tenant: Tenant
) -> None:
    assert score(client, tenant, payload("before")).status_code == 200
    live_rule(client, tenant, "mid", "decline", MID)
    assert (
        score(client, tenant, payload("after", event_time="2020-03-15T15:00:00Z")).status_code
        == 200
    )
    items = {
        i["transaction_id"]: i
        for i in client.get(
            "/api/v1/fraud/assessments", headers=tenant.headers(Role.VIEWER)
        ).json()["items"]
    }
    assert (
        items["before"]["decision"],
        items["before"]["policy_decision"],
        items["before"]["rules_status"],
    ) == (
        "allow",
        "allow",
        "none",
    )
    assert (
        items["after"]["decision"],
        items["after"]["policy_decision"],
        items["after"]["rules_status"],
    ) == (
        "decline",
        "allow",
        "evaluated",
    )


def test_list_source_filter(client: TestClient, tenant: Tenant) -> None:
    score(client, tenant, payload("r1", source="replay"))
    score(client, tenant, payload("s1", source="simulated", event_time="2020-03-15T15:00:00Z"))
    score(client, tenant, payload("l1", source="live", event_time="2020-03-15T16:00:00Z"))
    h = tenant.headers(Role.ANALYST)

    def ids(query: str) -> set[str]:
        body = client.get(f"/api/v1/fraud/assessments?{query}", headers=h).json()
        return {i["transaction_id"] for i in body["items"]}

    assert ids("") == {"r1", "s1", "l1"}
    assert ids("source=replay") == {"r1"}
    assert ids("source=replay&source=live") == {"r1", "l1"}
    assert ids("source=backfill") == set()
    r = client.get("/api/v1/fraud/assessments?source=production", headers=h)
    assert r.status_code == 422
