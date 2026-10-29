"""Phase 6: versioned rules engine (PostgreSQL; the stream parity test also needs Redis)."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from fraud_api.core.security import Role
from fraud_api.db.models import (
    AuditLog,
    RiskAssessment,
    RuleActivation,
    RuleDefinition,
    RuleEvaluation,
    Transaction,
    User,
)
from tests.fixtures import synthetic_events
from tests.integration.conftest import ROOT, AppFactory, Tenant, create_tenant
from tests.integration.test_scoring_api import T0, payload, score
from tests.integration.test_streaming import (  # noqa: F401 - pytest fixtures
    StreamEnv,
    ev,
    redis_url,
    stream_factory,
)

pytestmark = pytest.mark.integration

ALWAYS = {"field": "amount", "op": "gt", "value": 0}
LARGE = {"field": "amount", "op": "gte", "value": 900}
# The test policy already declines 950.00 with the test model but allows 125.40, so escalation
# tests use 125.40 and a rule matching amounts >= 100.
MID = {"field": "amount", "op": "gte", "value": 100}


def create(
    client: TestClient,
    tenant: Tenant,
    key: str,
    action: str,
    condition: Any = ALWAYS,
    role: Role = Role.ANALYST,
    priority: int = 100,
) -> Any:
    return client.post(
        "/api/v1/rules",
        json={
            "rule_key": key,
            "name": key.replace("_", " "),
            "priority": priority,
            "action": action,
            "condition": condition,
        },
        headers=tenant.headers(role),
    )


def activate(
    client: TestClient,
    tenant: Tenant,
    key: str,
    version: int,
    role: Role = Role.ADMIN,
    **body: Any,
) -> Any:
    return client.post(
        f"/api/v1/rules/{key}/versions/{version}/activate",
        json=body or None,
        headers=tenant.headers(role),
    )


def live_rule(client: TestClient, tenant: Tenant, key: str, action: str, condition: Any) -> int:
    r = create(client, tenant, key, action, condition)
    assert r.status_code == 201, r.text
    version: int = r.json()["version"]
    assert activate(client, tenant, key, version).status_code == 200
    return version


def audits(engine: Engine, org_id: uuid.UUID, prefix: str = "rule.") -> list[AuditLog]:
    with Session(engine) as s:
        return list(
            s.scalars(
                select(AuditLog)
                .where(AuditLog.organization_id == org_id, AuditLog.action.startswith(prefix))
                .order_by(AuditLog.id)
            )
        )


# ---------------------------------------------------------------- definitions and versions


def test_create_list_inspect_versions(client: TestClient, tenant: Tenant) -> None:
    r1 = create(client, tenant, "large_amount", "review", LARGE)
    assert r1.status_code == 201, r1.text
    v1 = r1.json()
    assert (v1["version"], v1["active"], v1["action"]) == (1, False, "review")
    assert len(v1["condition_sha256"]) == 64
    v2 = create(client, tenant, "large_amount", "decline", LARGE, priority=900).json()
    assert v2["version"] == 2 and v2["condition_sha256"] == v1["condition_sha256"]
    create(client, tenant, "other", "flag")

    h = tenant.headers(Role.VIEWER)
    listing = client.get("/api/v1/rules", headers=h).json()
    assert listing["active_count"] == 0 and listing["max_active"] == 100
    assert [
        (i["rule_key"], i["latest_version"], i["versions"], i["active_version"])
        for i in listing["items"]
    ] == [("large_amount", 2, 2, None), ("other", 1, 1, None)]
    detail = client.get("/api/v1/rules/large_amount", headers=h).json()
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    one = client.get("/api/v1/rules/large_amount/versions/1", headers=h).json()
    assert one["condition"] == LARGE and one["action"] == "review" and one["priority"] == 100
    assert client.get("/api/v1/rules/large_amount/versions/3", headers=h).status_code == 404
    assert client.get("/api/v1/rules/nope", headers=h).status_code == 404
    grammar = client.get("/api/v1/rules/grammar", headers=h).json()
    assert "amount" in grammar["fields"] and grammar["limits"]["max_depth"] == 4


def test_invalid_rules_are_rejected_without_echoing_values(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    r = create(client, tenant, "bad", "review", {"field": "is_fraud", "op": "eq", "value": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_rule"
    assert r.json()["error"]["details"][0]["path"] == "$.field"
    r = create(
        client, tenant, "bad", "review", {"field": "merchant_id", "op": "gt", "value": "SECRET"}
    )
    assert r.status_code == 422 and "SECRET" not in r.text
    assert create(client, tenant, "Bad Key!", "review").status_code == 422
    assert create(client, tenant, "ok", "allow").status_code == 422  # no relaxing action
    r = client.post(
        "/api/v1/rules",
        json={"rule_key": "x", "name": "x", "action": "flag", "condition": ALWAYS, "eval": "1"},
        headers=tenant.headers(Role.ANALYST),
    )
    assert r.status_code == 422
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(RuleDefinition)) == 0


def test_rule_versions_and_evaluations_are_immutable_in_the_database(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    live_rule(client, tenant, "large", "flag", ALWAYS)
    assert score(client, tenant, payload("t1")).status_code == 200
    for sql in (
        "UPDATE rule_definitions SET action = 'decline'",
        "UPDATE rule_definitions SET condition = '{}'::jsonb",
        "DELETE FROM rule_definitions",
        "UPDATE rule_evaluations SET outcome = 'not_matched'",
        "DELETE FROM rule_evaluations",
    ):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(sql))


def test_concurrent_creates_get_distinct_versions(client: TestClient, tenant: Tenant) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: create(client, tenant, "race", "flag"), range(8)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert sorted(r.json()["version"] for r in responses) == list(range(1, 9))


# ---------------------------------------------------------------- authorization and tenancy


def test_role_restrictions(client: TestClient, tenant: Tenant) -> None:
    assert create(client, tenant, "r", "flag", role=Role.VIEWER).status_code == 403
    assert create(client, tenant, "r", "flag", role=Role.SERVICE).status_code == 403
    assert create(client, tenant, "r", "flag", role=Role.ANALYST).status_code == 201
    assert create(client, tenant, "r", "flag", role=Role.ADMIN).status_code == 201
    for role in (Role.VIEWER, Role.ANALYST, Role.SERVICE):
        assert activate(client, tenant, "r", 1, role=role).status_code == 403
        r = client.post("/api/v1/rules/r/deactivate", headers=tenant.headers(role))
        assert r.status_code == 403
    for role in (Role.VIEWER, Role.ANALYST, Role.ADMIN):
        assert client.get("/api/v1/rules", headers=tenant.headers(role)).status_code == 200
        assert client.get("/api/v1/rules/r", headers=tenant.headers(role)).status_code == 200
    assert client.get("/api/v1/rules", headers=tenant.headers(Role.SERVICE)).status_code == 403
    assert client.get("/api/v1/rules").status_code == 401
    assert activate(client, tenant, "r", 2).status_code == 200


def test_tenant_isolation(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    live_rule(client, tenant, "decline_all", "decline", ALWAYS)
    other = create_tenant(engine, client, "globex")
    h = other.headers(Role.ADMIN)
    assert client.get("/api/v1/rules", headers=h).json()["items"] == []
    assert client.get("/api/v1/rules/decline_all", headers=h).status_code == 404
    assert activate(client, other, "decline_all", 1).status_code == 404
    assert client.post("/api/v1/rules/decline_all/deactivate", headers=h).status_code == 404
    # same key in the other tenant is an independent namespace
    assert create(client, other, "decline_all", "flag").json()["version"] == 1
    mine = score(client, tenant, payload("same")).json()
    theirs = score(client, other, payload("same")).json()
    assert mine["decision"] == "decline" and mine["rules"]["status"] == "evaluated"
    assert theirs["rules"]["status"] == "none" and theirs["decision"] == theirs["policy_decision"]
    assert mine["risk_score"] == theirs["risk_score"]


# ---------------------------------------------------------------- activation


def test_activation_switching_and_audit(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    create(client, tenant, "k", "review", LARGE)
    create(client, tenant, "k", "decline", LARGE)
    a1 = activate(client, tenant, "k", 1).json()
    assert (a1["active_version"], a1["previous_version"], a1["changed"]) == (1, None, True)
    a2 = activate(client, tenant, "k", 2).json()
    assert (a2["active_version"], a2["previous_version"], a2["changed"]) == (2, 1, True)
    again = activate(client, tenant, "k", 2).json()
    assert again["changed"] is False
    detail = client.get("/api/v1/rules/k", headers=tenant.headers(Role.VIEWER)).json()
    assert detail["active_version"] == 2
    assert [v["active"] for v in detail["versions"]] == [True, False]
    off = client.post("/api/v1/rules/k/deactivate", headers=tenant.headers(Role.ADMIN)).json()
    assert (off["active_version"], off["previous_version"], off["changed"]) == (None, 2, True)
    noop = client.post("/api/v1/rules/k/deactivate", headers=tenant.headers(Role.ADMIN)).json()
    assert noop["changed"] is False
    trail = [(a.action, a.details) for a in audits(engine, tenant.org_id)]
    assert [t[0] for t in trail] == [
        "rule.create",
        "rule.create",
        "rule.activate",
        "rule.activate",
        "rule.deactivate",
    ]
    assert trail[2][1] == {"rule_key": "k", "version": 1, "previous_version": None}
    assert trail[3][1] == {"rule_key": "k", "version": 2, "previous_version": 1}
    assert trail[4][1] == {"rule_key": "k", "version": None, "previous_version": 2}
    with Session(engine) as s:
        admin = s.scalar(select(User.id).where(User.email == "admin@acme.example.com"))
        assert {a.actor_user_id for a in audits(engine, tenant.org_id)[2:]} == {admin}


def test_concurrent_activations_are_serialized_and_fully_audited(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    for _ in range(6):
        create(client, tenant, "k", "flag")
    order = [1, 2, 3, 4, 5, 6, 2, 5, 1, 6, 3, 4]
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda v: activate(client, tenant, "k", v), order))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    changed = [r.json() for r in responses if r.json()["changed"]]
    trail = [a.details for a in audits(engine, tenant.org_id, "rule.activate")]
    assert len(trail) == len(changed)
    # The audit trail is a consistent chain: each activation saw the previous one's result.
    assert trail[0]["previous_version"] is None
    assert all(b["previous_version"] == a["version"] for a, b in itertools.pairwise(trail))
    with Session(engine) as s:
        rows = s.scalars(select(RuleActivation)).all()
        assert len(rows) == 1
        active = s.get(RuleDefinition, rows[0].rule_definition_id)
        assert active is not None and active.version == trail[-1]["version"]


def test_compare_and_set_guard_prevents_lost_updates(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    create(client, tenant, "k", "flag")
    create(client, tenant, "k", "review")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda v: activate(client, tenant, "k", v, expected_active_version="none"), [1, 2]
            )
        )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], [r.text for r in responses]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error"]["code"] == "activation_conflict"
    winner = next(r for r in responses if r.status_code == 200).json()["active_version"]
    assert len(audits(engine, tenant.org_id, "rule.activate")) == 1
    r = activate(client, tenant, "k", 3 - winner, expected_active_version=winner)
    assert r.status_code == 200 and r.json()["previous_version"] == winner


def test_active_rule_limit(
    client: TestClient, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fraud_api.rules.service.MAX_ACTIVE_RULES", 2)
    for key in ("a", "b", "c"):
        create(client, tenant, key, "flag")
    assert activate(client, tenant, "a", 1).status_code == 200
    assert activate(client, tenant, "b", 1).status_code == 200
    r = activate(client, tenant, "c", 1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "too_many_active_rules"
    create(client, tenant, "a", "review")
    assert activate(client, tenant, "a", 2).status_code == 200  # replacing a version is fine


# ---------------------------------------------------------------- scoring with rules


def test_no_active_rules_keeps_existing_decisions(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    create(client, tenant, "inactive", "decline", ALWAYS)  # created, never activated
    body = score(client, tenant, payload("t1", amount="950.00")).json()
    assert body["rules"] == {
        "status": "none",
        "evaluated": 0,
        "matched": [],
        "errors": [],
        "decided_by": None,
    }
    assert body["decision"] == body["policy_decision"]
    assert not any(r.startswith("rule") for r in body["reasons"])
    live_rule(client, tenant, "temp", "decline", ALWAYS)
    client.post("/api/v1/rules/temp/deactivate", headers=tenant.headers(Role.ADMIN))
    body = score(client, tenant, payload("t2", event_time="2020-03-15T15:00:00Z")).json()
    assert body["rules"]["status"] == "none" and body["decision"] == body["policy_decision"]
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(RuleEvaluation)) == 0


def test_rules_escalate_but_never_change_the_model_score(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    other = create_tenant(engine, client, "control")
    live_rule(client, tenant, "large", "decline", MID)
    live_rule(
        client,
        tenant,
        "net_flag",
        "flag",
        {"field": "channel", "op": "eq", "value": "card_not_present"},
    )
    live_rule(client, tenant, "small", "review", {"field": "amount", "op": "lt", "value": 10})
    body = payload("t1")
    mine = score(client, tenant, body).json()
    control = score(client, other, body).json()
    assert mine["risk_score"] == control["risk_score"]
    assert mine["policy_decision"] == control["policy_decision"] == control["decision"] == "allow"
    assert mine["decision"] == "decline"
    assert {m["rule_key"] for m in mine["rules"]["matched"]} == {"large", "net_flag"}
    assert mine["rules"]["decided_by"]["rule_key"] == "large"
    assert mine["rules"]["evaluated"] == 3
    assert "rule large@v1: decline" in mine["reasons"]
    assert "rule net_flag@v1: flag" in mine["reasons"]
    detail = client.get(
        f"/api/v1/fraud/predictions/{mine['assessment_id']}", headers=tenant.headers(Role.VIEWER)
    ).json()
    assert [
        (e["rule_key"], e["version"], e["outcome"], e["influenced_decision"])
        for e in detail["rule_evaluations"]
    ] == [
        ("large", 1, "matched", True),
        ("net_flag", 1, "matched", False),
        ("small", 1, "not_matched", False),
    ]
    with Session(engine) as s:
        ra = s.scalar(
            select(RiskAssessment).where(RiskAssessment.id == uuid.UUID(mine["assessment_id"]))
        )
        assert ra is not None
        assert (ra.decision, ra.policy_decision, ra.rules_status) == (
            "decline",
            control["policy_decision"],
            "evaluated",
        )
        audit = s.scalar(
            select(AuditLog).where(
                AuditLog.action == "fraud.score", AuditLog.resource_id == mine["assessment_id"]
            )
        )
        assert audit is not None and audit.details["decided_by"] == "large@v1"
        assert audit.details["rules_matched"] == 2


def test_idempotent_retry_returns_original_decision_after_rule_change(
    client: TestClient, tenant: Tenant
) -> None:
    body = payload("t1")
    first = score(client, tenant, body).json()
    assert first["rules"]["status"] == "none" and first["decision"] == "allow"
    live_rule(client, tenant, "large", "decline", MID)
    retry = score(client, tenant, body).json()
    assert retry["idempotent_replay"] is True
    for k in ("assessment_id", "decision", "policy_decision", "rules", "risk_score", "reasons"):
        assert retry[k] == first[k]

    second = score(client, tenant, payload("t2", event_time="2020-03-15T15:00:00Z"))
    s2 = second.json()
    assert s2["decision"] == "decline" and s2["rules"]["decided_by"]["version"] == 1
    # new version and deactivation do not rewrite history either
    create(client, tenant, "large", "flag", MID)
    activate(client, tenant, "large", 2)
    replay = score(client, tenant, payload("t2", event_time="2020-03-15T15:00:00Z")).json()
    assert replay["idempotent_replay"] is True
    assert replay["decision"] == "decline" and replay["rules"] == s2["rules"]


def _plant_broken_rule(engine: Engine, tenant: Tenant, action: str) -> None:
    """A stored condition that does not compile (only possible by bypassing the API)."""
    with Session(engine) as s:
        admin = s.scalar(
            select(User.id).where(User.organization_id == tenant.org_id, User.role == "admin")
        )
        d = RuleDefinition(
            organization_id=tenant.org_id,
            rule_key="broken",
            version=1,
            name="broken",
            priority=100,
            action=action,
            condition={"field": "not_a_field", "op": "eq", "value": 1},
            condition_sha256="0" * 64,
            created_by=admin,
        )
        s.add(d)
        s.flush()
        s.add(
            RuleActivation(
                organization_id=tenant.org_id,
                rule_key="broken",
                rule_definition_id=d.id,
                activated_by=admin,
                activated_at=datetime.now(UTC),
            )
        )
        s.commit()


@pytest.mark.parametrize(("action", "expected"), [("flag", "review"), ("decline", "decline")])
def test_rule_failure_escalates_and_is_recorded(
    client: TestClient, tenant: Tenant, engine: Engine, action: str, expected: str
) -> None:
    _plant_broken_rule(engine, tenant, action)
    body = score(client, tenant, payload("t1", amount="5.00")).json()
    assert body["status"] == "scored" and body["rules"]["status"] == "error"
    assert body["decision"] == expected != "allow"
    assert body["rules"]["errors"][0]["rule_key"] == "broken"
    assert "rule_error broken@v1: escalated" in body["reasons"]
    with Session(engine) as s:
        e = s.scalars(select(RuleEvaluation)).one()
        assert (e.outcome, e.error_type, e.influenced_decision) == ("error", "ValueError", True)


def test_runtime_rule_exception_escalates(
    client: TestClient, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_rule(client, tenant, "flaky", "flag", ALWAYS)

    def boom(self: Any, facts: Any) -> bool:
        raise ZeroDivisionError

    monkeypatch.setattr("fraud_api.rules.grammar.Predicate.evaluate", boom)
    body = score(client, tenant, payload("t1", amount="5.00")).json()
    assert body["rules"]["status"] == "error" and body["decision"] in ("review", "decline")


def test_database_rejects_lenient_or_relaxed_decisions(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    _plant_broken_rule(engine, tenant, "flag")
    score(client, tenant, payload("err", amount="5.00"))
    for sql in (
        "UPDATE risk_assessments SET decision = 'allow' WHERE rules_status = 'error'",
        "UPDATE risk_assessments SET policy_decision = 'decline', decision = 'review'",
        "UPDATE risk_assessments SET rules_status = 'none', policy_decision = 'allow'",
    ):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="ck_risk_assessments"):
            conn.execute(text(sql))


def test_degraded_score_makes_score_rules_unknown(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    live_rule(
        client, tenant, "score_high", "decline", {"field": "risk_score", "op": "gte", "value": 0.5}
    )
    live_rule(client, tenant, "no_score", "decline", {"field": "risk_score", "op": "is_null"})
    body = score(client, tenant, payload("t1")).json()
    assert body["status"] == "degraded" and body["policy_decision"] == "review"
    assert body["decision"] == "decline" and body["rules"]["decided_by"]["rule_key"] == "no_score"
    detail = client.get(
        f"/api/v1/fraud/predictions/{body['assessment_id']}", headers=tenant.headers(Role.VIEWER)
    ).json()
    outcomes = {e["rule_key"]: e["outcome"] for e in detail["rule_evaluations"]}
    assert outcomes == {"score_high": "unknown", "no_score": "matched"}


# ---------------------------------------------------------------- parity across paths

PARITY_RULES: list[tuple[str, str, int, dict[str, Any]]] = [
    ("big_amount", "review", 500, {"field": "amount", "op": "gte", "value": 300}),
    (
        "busy_customer",
        "challenge",
        400,
        {"field": "feature.cust_txn_count_24h", "op": "gte", "value": 3},
    ),
    (
        "risky_online",
        "decline",
        300,
        {
            "all": [
                {"field": "risk_score", "op": "gte", "value": 0.05},
                {"field": "channel", "op": "eq", "value": "card_not_present"},
            ]
        },
    ),
    (
        "rapid_repeat",
        "review",
        200,
        {"field": "feature.seconds_since_prev_txn", "op": "lt", "value": 3600},
    ),
    (
        "everyday",
        "flag",
        100,
        {"field": "category", "op": "in", "value": ["grocery_pos", "gas_transport"]},
    ),
]


def _trace(engine: Engine, org_id: uuid.UUID) -> dict[str, tuple[Any, ...]]:
    with Session(engine) as s:
        rows = s.execute(
            select(Transaction.external_id, RiskAssessment)
            .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
            .where(Transaction.organization_id == org_id)
        ).all()
        evals: dict[uuid.UUID, list[tuple[Any, ...]]] = {}
        for e, d in s.execute(
            select(RuleEvaluation, RuleDefinition)
            .join(RuleDefinition, RuleDefinition.id == RuleEvaluation.rule_definition_id)
            .where(RuleEvaluation.organization_id == org_id)
        ).all():
            evals.setdefault(e.risk_assessment_id, []).append(
                (d.rule_key, d.version, e.outcome, e.influenced_decision)
            )
        return {
            ext: (
                ra.risk_score,
                ra.features,
                ra.policy_decision,
                ra.decision,
                ra.rules_status,
                ra.reasons,
                tuple(sorted(evals.get(ra.id, []))),
            )
            for ext, ra in rows
        }


def test_rules_parity_across_single_batch_and_stream(
    stream_factory: Callable[..., StreamEnv],  # noqa: F811 - imported fixture
) -> None:
    env = stream_factory()
    tenants = {name: env.tenant(name) for name in ("single", "batch", "stream")}
    for t in tenants.values():
        for key, action, priority, condition in PARITY_RULES:
            assert (
                create(env.client, t, key, action, condition, priority=priority).status_code == 201
            )
            assert activate(env.client, t, key, 1).status_code == 200
    data = synthetic_events(n=120, n_customers=6, seed=11)
    items = sorted(
        (
            ev(
                r.event_id,
                customer=r.customer_id,
                amount=f"{r.amount:.2f}",
                category=r.category,
                event_time=r.event_time.isoformat(),
            )
            for r in data.itertuples()
        ),
        key=lambda e: (e["event_time"], e["transaction_id"]),
    )
    for item in items:
        assert score(env.client, tenants["single"], item).status_code == 200
    batch = env.client.post(
        "/api/v1/fraud/batch", json={"items": items}, headers=tenants["batch"].headers()
    ).json()
    assert batch["complete"] and batch["summary"]["scored"] == len(items)
    for k in range(0, len(items), 40):
        assert env.ingest(tenants["stream"], items[k : k + 40]).status_code == 202
    env.drain(env.worker("w1", shards=[0, 1]), env.worker("w2", shards=[2, 3]))

    single, batched, streamed = (_trace(env.engine, t.org_id) for t in tenants.values())
    assert len(single) == len(items)
    assert single == batched == streamed
    # The fixture exercises every outcome and an escalation, so parity is not vacuous.
    outcomes = {o for v in single.values() for (_, _, o, _) in v[6]}
    assert outcomes == {"matched", "not_matched", "unknown"}
    assert any(v[3] != v[2] for v in single.values())
    assert any(v[3] == v[2] for v in single.values())


# ---------------------------------------------------------------- migration


def test_migration_backfills_existing_assessments(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    for i, amount in enumerate(["5.00", "950.00"]):
        score(
            client,
            tenant,
            payload(f"old{i}", amount=amount, event_time=T0.replace(minute=i).isoformat()),
        )
    with Session(engine) as s:
        before = dict(s.execute(select(RiskAssessment.id, RiskAssessment.decision)).all())
    cfg = Config(str(ROOT / "apps/api/alembic.ini"))
    command.downgrade(cfg, "0002")
    try:
        with engine.connect() as conn:
            cols = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'risk_assessments'"
                    )
                )
            }
        assert "policy_decision" not in cols and "rules_status" not in cols
    finally:
        command.upgrade(cfg, "head")
    with Session(engine) as s:
        after = s.execute(
            select(
                RiskAssessment.id,
                RiskAssessment.decision,
                RiskAssessment.policy_decision,
                RiskAssessment.rules_status,
            )
        ).all()
    assert {r.id: r.decision for r in after} == before
    assert all(r.policy_decision == r.decision and r.rules_status == "none" for r in after)
