"""Phase 6 analytics endpoints against known database fixtures (PostgreSQL)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fraud_api.core.security import Role
from fraud_api.db.models import (
    RiskAssessment,
    RuleDefinition,
    RuleEvaluation,
    Transaction,
    User,
)
from tests.integration.conftest import Tenant, create_tenant
from tests.integration.test_scoring_api import payload, score

pytestmark = pytest.mark.integration

S0 = datetime(2026, 10, 1, tzinfo=UTC)  # processing time base
E0 = datetime(2020, 3, 15, tzinfo=UTC)  # event time base
H = timedelta(hours=1)


def _txn(
    s: Session, org: uuid.UUID, ext: str, source: str, event: datetime, received: datetime
) -> Transaction:
    t = Transaction(
        organization_id=org,
        external_id=ext,
        customer_id="cus_1",
        amount=Decimal("10.00"),
        currency="USD",
        category="travel",
        channel="unspecified",
        event_time=event,
        source=source,
        schema_version="1.0",
        payload_sha256="0" * 64,
        received_at=received,
    )
    s.add(t)
    s.flush()
    return t


def _assess(
    s: Session,
    t: Transaction,
    scored_at: datetime,
    decision: str,
    policy_decision: str,
    level: str,
    rules_status: str,
    timings: dict[str, float],
    status: str = "scored",
) -> RiskAssessment:
    ra = RiskAssessment(
        organization_id=t.organization_id,
        transaction_id=t.id,
        status=status,
        risk_score=0.5 if status == "scored" else None,
        risk_level=level,
        decision=decision,
        policy_decision=policy_decision,
        rules_status=rules_status,
        decision_mode="sandbox",
        reasons=[],
        policy_version="p",
        timings_ms=timings,
        scored_at=scored_at,
    )
    s.add(ra)
    s.flush()
    return ra


@pytest.fixture
def seeded(client: TestClient, tenant: Tenant, engine: Engine) -> dict[str, Any]:
    """Known rows. Window W = [S0, S0+24h) on the processed basis:

    #  source     received/scored  event     status    decision/policy   level    rules     total
    1  live       S0+10m           E0+1h     scored    allow/allow       low      none      10
    2  live       S0+20m           E0+2h     scored    decline/allow     low      evaluated 20
    3  replay     S0+1h05m         E0+2h30m  scored    review/review     high     evaluated 30
    4  simulated  S0+2h            E0+5h     degraded  review/review     unknown  none      40
    5  replay     S0+25h (outside) E0+3h     scored    challenge/chall.  medium   evaluated 50
    6  backfill   S0+30m           E0+1h30m  (imported, no assessment)
    7  live       S0+24h (= end)   E0+10h    scored    allow/allow       low      none      70
    other tenant: 1 live row at S0+10m, decline/decline.
    """
    other = create_tenant(engine, client, "globex")
    with Session(engine) as s:
        admin = s.scalar(
            select(User.id).where(User.organization_id == tenant.org_id, User.role == "admin")
        )
        org = tenant.org_id

        def row(
            ext: str, source: str, offset: timedelta, event: timedelta, *a: Any, **kw: Any
        ) -> RiskAssessment:
            t = _txn(s, org, ext, source, E0 + event, S0 + offset)
            return _assess(s, t, S0 + offset, *a, **kw)

        full = {"features": 1.0, "inference": 0.5}
        row(
            "r1",
            "live",
            timedelta(minutes=10),
            H,
            "allow",
            "allow",
            "low",
            "none",
            {"total": 10.0, **full},
        )
        r2 = row(
            "r2",
            "live",
            timedelta(minutes=20),
            2 * H,
            "decline",
            "allow",
            "low",
            "evaluated",
            {"total": 20.0, "features": 2.0, "inference": 0.5, "rules": 0.1},
        )
        r3 = row(
            "r3",
            "replay",
            timedelta(minutes=65),
            2.5 * H,
            "review",
            "review",
            "high",
            "evaluated",
            {"total": 30.0, "features": 3.0, "inference": 0.5, "rules": 0.3},
        )
        row(
            "r4",
            "simulated",
            2 * H,
            5 * H,
            "review",
            "review",
            "unknown",
            "none",
            {"total": 40.0},
            status="degraded",
        )
        r5 = row(
            "r5",
            "replay",
            25 * H,
            3 * H,
            "challenge",
            "challenge",
            "medium",
            "evaluated",
            {"total": 50.0, **full, "rules": 0.2},
        )
        _txn(s, org, "r6", "backfill", E0 + 1.5 * H, S0 + timedelta(minutes=30))
        row("r7", "live", 24 * H, 10 * H, "allow", "allow", "low", "none", {"total": 70.0, **full})
        t = _txn(s, other.org_id, "o1", "live", E0 + H, S0 + timedelta(minutes=10))
        _assess(
            s,
            t,
            S0 + timedelta(minutes=10),
            "decline",
            "decline",
            "critical",
            "none",
            {"total": 99.0},
        )

        rules = {}
        for key, action in (("r_decline", "decline"), ("r_flag", "flag")):
            d = RuleDefinition(
                organization_id=org,
                rule_key=key,
                version=1,
                name=key,
                priority=100,
                action=action,
                condition={"field": "amount", "op": "gt", "value": 0},
                condition_sha256="0" * 64,
                created_by=admin,
            )
            s.add(d)
            s.flush()
            rules[key] = d

        def ev(ra: RiskAssessment, key: str, outcome: str, influenced: bool = False) -> None:
            s.add(
                RuleEvaluation(
                    organization_id=org,
                    risk_assessment_id=ra.id,
                    rule_definition_id=rules[key].id,
                    rule_key=key,
                    outcome=outcome,
                    influenced_decision=influenced,
                )
            )

        ev(r2, "r_decline", "matched", True)
        ev(r2, "r_flag", "not_matched")
        ev(r3, "r_decline", "unknown")
        ev(r3, "r_flag", "matched")
        ev(r5, "r_decline", "matched")  # outside the window
        s.commit()
        return {"other": other, "rules": {k: v.id for k, v in rules.items()}}


def get(
    client: TestClient, tenant: Tenant, path: str, role: Role = Role.VIEWER, **params: Any
) -> Any:
    return client.get(f"/api/v1/fraud/insights/{path}", params=params, headers=tenant.headers(role))


W = {"start": S0.isoformat(), "end": (S0 + 24 * H).isoformat()}


def test_overview_counts_rates_and_denominators(
    client: TestClient, tenant: Tenant, seeded: dict[str, Any]
) -> None:
    r = get(client, tenant, "overview", **W)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["window"] == {
        "start": "2026-10-01T00:00:00Z",
        "end": "2026-10-02T00:00:00Z",
        "basis": "processed",
        "sources": ["live", "replay", "simulated", "backfill"],
    }
    assert o["transactions"] == {"received": 5, "scored": 4, "unscored": 1}
    a = o["assessments"]
    assert a["total"] == 4
    assert a["by_status"] == {"scored": 3, "degraded": 1}
    assert a["by_decision"] == {"allow": 1, "challenge": 0, "review": 2, "decline": 1}
    assert a["by_policy_decision"] == {"allow": 2, "challenge": 0, "review": 2, "decline": 0}
    assert a["by_risk_level"] == {"low": 2, "medium": 0, "high": 1, "critical": 0, "unknown": 1}
    assert a["by_source"] == {"live": 2, "replay": 1, "simulated": 1, "backfill": 0}
    assert a["by_rules_status"] == {"none": 2, "evaluated": 2, "error": 0}
    assert o["alert_rate"] == {"numerator": 3, "denominator": 4, "rate": 0.75}
    assert o["policy_alert_rate"] == {"numerator": 2, "denominator": 4, "rate": 0.5}
    assert o["rule_escalation_rate"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert o["degraded_rate"] == {"numerator": 1, "denominator": 4, "rate": 0.25}


def test_source_filters(client: TestClient, tenant: Tenant, seeded: dict[str, Any]) -> None:
    live = get(client, tenant, "overview", source="live", **W).json()
    assert live["window"]["sources"] == ["live"]
    assert live["transactions"]["received"] == 2
    assert live["alert_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    both = get(client, tenant, "overview", source=["replay", "simulated"], **W).json()
    assert both["assessments"]["total"] == 2 and both["window"]["sources"] == [
        "replay",
        "simulated",
    ]
    backfill = get(client, tenant, "overview", source="backfill", **W).json()
    assert backfill["transactions"] == {"received": 1, "scored": 0, "unscored": 1}
    assert backfill["assessments"]["total"] == 0
    assert backfill["alert_rate"] == {"numerator": 0, "denominator": 0, "rate": None}
    assert get(client, tenant, "overview", source="production", **W).status_code == 422


def test_event_time_basis(client: TestClient, tenant: Tenant, seeded: dict[str, Any]) -> None:
    o = get(
        client,
        tenant,
        "overview",
        basis="event",
        start=E0.isoformat(),
        end=(E0 + 4 * H).isoformat(),
    ).json()
    assert o["transactions"]["received"] == 5  # r1, r2, r3, r5, r6
    assert o["assessments"]["total"] == 4  # r1, r2, r3, r5
    assert o["assessments"]["by_decision"]["challenge"] == 1


def test_empty_window_and_tenant_isolation(
    client: TestClient, tenant: Tenant, seeded: dict[str, Any]
) -> None:
    empty = get(
        client, tenant, "overview", start=(S0 - 48 * H).isoformat(), end=(S0 - 24 * H).isoformat()
    ).json()
    assert empty["transactions"] == {"received": 0, "scored": 0, "unscored": 0}
    assert empty["assessments"]["total"] == 0
    assert all(empty[k]["rate"] is None for k in ("alert_rate", "rule_escalation_rate"))
    lat = get(
        client, tenant, "latency", start=(S0 - 48 * H).isoformat(), end=(S0 - 24 * H).isoformat()
    ).json()
    assert lat["processing_ms"] == {
        "count": 0,
        "mean": None,
        "p50": None,
        "p90": None,
        "p95": None,
        "p99": None,
        "max": None,
    }
    hits = get(
        client, tenant, "rule-hits", start=(S0 - 48 * H).isoformat(), end=(S0 - 24 * H).isoformat()
    ).json()
    assert hits["assessments"] == 0 and hits["rules"] == []

    other = seeded["other"]
    o = get(client, other, "overview", **W).json()
    assert o["assessments"]["total"] == 1 and o["assessments"]["by_decision"]["decline"] == 1
    assert get(client, other, "rule-hits", **W).json()["rules"] == []
    assert get(client, other, "latency", **W).json()["processing_ms"]["max"] == 99.0


def test_volume_buckets(client: TestClient, tenant: Tenant, seeded: dict[str, Any]) -> None:
    v = get(
        client, tenant, "volume", bucket="hour", start=S0.isoformat(), end=(S0 + 3 * H).isoformat()
    ).json()
    assert [
        (b["start"], b["transactions"], b["assessments"], b["alerts"]) for b in v["buckets"]
    ] == [
        ("2026-10-01T00:00:00Z", 3, 2, 1),
        ("2026-10-01T01:00:00Z", 1, 1, 1),
        ("2026-10-01T02:00:00Z", 1, 1, 1),
    ]
    assert v["buckets"][0]["by_decision"] == {"allow": 1, "challenge": 0, "review": 0, "decline": 1}
    day = get(
        client, tenant, "volume", bucket="day", start=S0.isoformat(), end=(S0 + 72 * H).isoformat()
    ).json()
    assert [(b["transactions"], b["assessments"]) for b in day["buckets"]] == [
        (5, 4),
        (2, 2),
        (0, 0),  # day 2: r5 (S0+25h) and r7 (S0+24h)
    ]
    live = get(client, tenant, "volume", bucket="day", source="live", **W).json()
    assert live["buckets"][0]["assessments"] == 2


def test_rule_hits(client: TestClient, tenant: Tenant, seeded: dict[str, Any]) -> None:
    h = get(client, tenant, "rule-hits", **W).json()
    assert (
        h["assessments"],
        h["assessments_with_rules"],
        h["assessments_with_match"],
        h["assessments_escalated"],
    ) == (4, 2, 2, 1)
    rows = {r["rule_key"]: r for r in h["rules"]}
    assert rows["r_decline"] | {"rule_id": None} == {
        "rule_id": None,
        "rule_key": "r_decline",
        "version": 1,
        "action": "decline",
        "evaluated": 2,
        "matched": 1,
        "unknown": 1,
        "errors": 0,
        "influenced": 1,
        "match_rate": 0.5,
    }
    assert rows["r_decline"]["rule_id"] == str(seeded["rules"]["r_decline"])
    assert (rows["r_flag"]["evaluated"], rows["r_flag"]["matched"]) == (2, 1)
    # wider window includes r5's match
    wide = get(
        client, tenant, "rule-hits", start=S0.isoformat(), end=(S0 + 48 * H).isoformat()
    ).json()
    assert {r["rule_key"]: r["matched"] for r in wide["rules"]} == {"r_decline": 2, "r_flag": 1}
    replay = get(client, tenant, "rule-hits", source="replay", **W).json()
    assert {r["rule_key"]: r["evaluated"] for r in replay["rules"]} == {"r_decline": 1, "r_flag": 1}


def test_latency_percentiles_match_linear_interpolation(
    client: TestClient, tenant: Tenant, seeded: dict[str, Any]
) -> None:
    lat = get(client, tenant, "latency", **W).json()
    totals = np.array([10.0, 20.0, 30.0, 40.0])
    p = lat["processing_ms"]
    assert p["count"] == 4 and p["mean"] == pytest.approx(25.0) and p["max"] == 40.0
    for name, q in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
        assert p[name] == pytest.approx(float(np.percentile(totals, q)))
    c = lat["components_ms"]
    assert c["features"]["count"] == 3 and c["features"]["p50"] == pytest.approx(2.0)
    assert c["inference"]["count"] == 3
    assert c["rules"]["count"] == 2 and c["rules"]["max"] == pytest.approx(0.3)


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2026-10-01T00:00:00", "end": "2026-10-02T00:00:00Z"},  # naive
        {"start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"},  # reversed
        {"start": "2026-10-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},  # empty
        {"start": "2025-01-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},  # > 366 days
    ],
)
def test_invalid_windows(client: TestClient, tenant: Tenant, params: dict[str, str]) -> None:
    for path in ("overview", "volume", "rule-hits", "latency"):
        r = get(client, tenant, path, **params)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_window", path


def test_volume_alignment_and_bucket_limit(client: TestClient, tenant: Tenant) -> None:
    r = get(
        client,
        tenant,
        "volume",
        bucket="hour",
        start="2026-10-01T00:30:00Z",
        end="2026-10-01T02:00:00Z",
    )
    assert r.status_code == 400 and "aligned" in r.json()["error"]["message"]
    r = get(
        client,
        tenant,
        "volume",
        bucket="day",
        start="2026-10-01T01:00:00Z",
        end="2026-10-03T00:00:00Z",
    )
    assert r.status_code == 400
    r = get(
        client,
        tenant,
        "volume",
        bucket="hour",
        start="2026-01-01T00:00:00Z",
        end="2026-03-01T00:00:00Z",
    )
    assert r.status_code == 400 and "buckets" in r.json()["error"]["message"]
    # an offset is converted to UTC before the alignment check
    r = get(
        client,
        tenant,
        "volume",
        bucket="hour",
        start="2026-10-01T02:00:00+02:00",
        end="2026-10-01T03:00:00+02:00",
    )
    assert r.status_code == 200 and r.json()["buckets"][0]["start"] == "2026-10-01T00:00:00Z"


def test_authorization(client: TestClient, tenant: Tenant) -> None:
    for path in ("overview", "volume", "rule-hits", "latency"):
        assert get(client, tenant, path, role=Role.SERVICE, **W).status_code == 403
        for role in (Role.VIEWER, Role.ANALYST, Role.ADMIN):
            assert get(client, tenant, path, role=role, **W).status_code == 200
        assert client.get(f"/api/v1/fraud/insights/{path}", params=W).status_code == 401


def test_aggregates_reflect_scored_requests(client: TestClient, tenant: Tenant) -> None:
    for i, amount in enumerate(["5.00", "300.00", "950.00"]):
        assert (
            score(
                client, tenant, payload(f"t{i}", amount=amount, customer_id=f"cus_{i:016x}")
            ).status_code
            == 200
        )
    now = datetime.now(UTC)
    window = {"start": (now - H).isoformat(), "end": (now + H).isoformat()}
    o = get(client, tenant, "overview", **window).json()
    assert o["assessments"]["total"] == 3 and o["transactions"]["scored"] == 3
    assert o["assessments"]["by_decision"] == {
        "allow": 1,
        "challenge": 0,
        "review": 1,
        "decline": 1,
    }
    assert o["alert_rate"]["rate"] == pytest.approx(2 / 3)
    lat = get(client, tenant, "latency", **window).json()
    assert lat["processing_ms"]["count"] == 3 and lat["components_ms"]["rules"]["count"] == 3
    assert lat["processing_ms"]["p50"] > 0
