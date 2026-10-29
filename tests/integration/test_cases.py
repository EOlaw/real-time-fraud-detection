"""Phase 8: investigation cases (PostgreSQL)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from fraud_api.core.security import Role, hash_password
from fraud_api.db.models import (
    AuditLog,
    Case,
    CaseEvent,
    CaseNote,
    ModelVersion,
    RiskAssessment,
    User,
)
from tests.integration.conftest import PASSWORD, AppFactory, Tenant, create_tenant
from tests.integration.test_scoring_api import payload, score

pytestmark = pytest.mark.integration


def scored(client: TestClient, tenant: Tenant, txn: str = "t1", **kw: Any) -> dict[str, Any]:
    r = score(client, tenant, payload(txn, **kw))
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def open_case(
    client: TestClient, tenant: Tenant, assessment_id: str, role: Role = Role.ANALYST, **kw: Any
) -> Any:
    return client.post(
        "/api/v1/cases",
        json={"assessment_id": assessment_id, **kw},
        headers=tenant.headers(role),
    )


def act(
    client: TestClient, tenant: Tenant, case_id: str, verb: str, role: Role, **body: Any
) -> Any:
    return client.post(f"/api/v1/cases/{case_id}/{verb}", json=body, headers=tenant.headers(role))


def user_id(engine: Engine, tenant: Tenant, role: str) -> str:
    with Session(engine) as s:
        return str(
            s.scalar(
                select(User.id).where(User.organization_id == tenant.org_id, User.role == role)
            )
        )


def second_analyst(engine: Engine, client: TestClient, tenant: Tenant) -> tuple[str, str]:
    with Session(engine) as s:
        u = User(
            organization_id=tenant.org_id,
            email="analyst2@acme.example.com",
            password_hash=hash_password(PASSWORD),
            role="analyst",
        )
        s.add(u)
        s.commit()
        uid = str(u.id)
    r = client.post(
        "/api/v1/auth/token", json={"email": "analyst2@acme.example.com", "password": PASSWORD}
    )
    return uid, r.json()["access_token"]


def assessment_row(engine: Engine, aid: str) -> tuple[Any, ...]:
    with Session(engine) as s:
        ra = s.get(RiskAssessment, uuid.UUID(aid))
        assert ra is not None
        return (ra.risk_score, ra.policy_decision, ra.decision, ra.status, ra.rules_status)


# ---------------------------------------------------------------- open and duplicates


def test_open_case_is_idempotent_and_audited(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant, amount="950.00")
    r = open_case(client, tenant, a["assessment_id"], note="Large online payment")
    assert r.status_code == 201, r.text
    body = r.json()
    case = body["case"]
    assert body["created"] is True
    assert (case["status"], case["version"], case["severity"]) == ("new", 1, "critical")
    assert case["transaction_id"] == "t1" and case["source"] == "replay"
    assert case["risk_score"] == a["risk_score"] and case["decision"] == a["decision"]
    assert [e["kind"] for e in case["events"]] == ["created"]
    assert [n["body"] for n in case["notes"]] == ["Large online payment"]
    again = open_case(client, tenant, a["assessment_id"])
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["case"]["id"] == case["id"]
    with Session(engine) as s:
        audit = s.scalars(select(AuditLog).where(AuditLog.action == "case.create")).all()
        assert len(audit) == 1 and audit[0].resource_id == case["id"]


def test_concurrent_open_creates_one_case(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rs = list(pool.map(lambda _: open_case(client, tenant, a["assessment_id"]), range(8)))
    assert sorted(r.status_code for r in rs).count(201) == 1, [r.text for r in rs]
    assert all(r.status_code in (200, 201) for r in rs)
    assert len({r.json()["case"]["id"] for r in rs}) == 1
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Case)) == 1
        assert s.scalar(select(func.count()).select_from(CaseEvent)) == 1


def test_degraded_assessment_case_is_high_severity_and_keeps_review(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    a = scored(client, tenant)
    assert a["status"] == "degraded"
    case = open_case(client, tenant, a["assessment_id"]).json()["case"]
    assert case["severity"] == "high" and case["assessment_status"] == "degraded"
    assert case["risk_score"] is None and case["decision"] == "review"
    assert case["model"]["version"] is None and case["model"]["serving"] is False


def test_inactive_model_version_is_reported(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    case = open_case(client, tenant, a["assessment_id"]).json()["case"]
    assert case["model"]["serving"] is True
    # Simulate an assessment scored by a version this instance no longer serves.
    with Session(engine) as s:
        old = ModelVersion(
            name="test-model",
            version="0.0.0-old",
            artifact_sha256="0" * 64,
            manifest={},
            status="retired",
        )
        s.add(old)
        s.flush()
        s.execute(
            text("UPDATE risk_assessments SET model_version_id = :m WHERE id = :a"),
            {"m": old.id, "a": a["assessment_id"]},
        )
        s.commit()
    detail = client.get(f"/api/v1/cases/{case['id']}", headers=tenant.headers(Role.VIEWER)).json()
    assert detail["model"] == {
        "name": "test-model",
        "version": "0.0.0-old",
        "registry_status": "retired",
        "serving": False,
    }
    exp = client.get(
        f"/api/v1/fraud/predictions/{a['assessment_id']}/explanation",
        headers=tenant.headers(Role.VIEWER),
    )
    assert exp.status_code == 409 and exp.json()["error"]["code"] == "model_mismatch"


# ---------------------------------------------------------------- roles and tenancy


def test_role_enforcement(client: TestClient, tenant: Tenant) -> None:
    a = scored(client, tenant)
    assert open_case(client, tenant, a["assessment_id"], Role.VIEWER).status_code == 403
    assert open_case(client, tenant, a["assessment_id"], Role.SERVICE).status_code == 403
    case = open_case(client, tenant, a["assessment_id"], Role.ADMIN).json()["case"]
    cid = case["id"]
    for role, expected in [
        (Role.VIEWER, 200),
        (Role.ANALYST, 200),
        (Role.ADMIN, 200),
        (Role.SERVICE, 403),
    ]:
        assert client.get(f"/api/v1/cases/{cid}", headers=tenant.headers(role)).status_code == (
            expected
        )
        assert client.get("/api/v1/cases", headers=tenant.headers(role)).status_code == expected
    assert client.get("/api/v1/cases").status_code == 401
    for role in (Role.VIEWER, Role.SERVICE):
        assert act(client, tenant, cid, "notes", role, body="x").status_code == 403
        assert (
            act(
                client, tenant, cid, "assign", role, assignee_id=None, expected_version=1
            ).status_code
            == 403
        )
        assert (
            act(
                client, tenant, cid, "status", role, status="under_review", expected_version=1
            ).status_code
            == 403
        )
        assert (
            act(
                client, tenant, cid, "resolve", role, outcome="fraud", expected_version=1
            ).status_code
            == 403
        )


def test_tenant_isolation(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    other = create_tenant(engine, client, "globex")
    h = other.headers(Role.ADMIN)
    assert client.get(f"/api/v1/cases/{cid}", headers=h).status_code == 404
    assert client.get("/api/v1/cases", headers=h).json()["total"] == 0
    assert open_case(client, other, a["assessment_id"], Role.ADMIN).status_code == 404
    assert act(client, other, cid, "notes", Role.ADMIN, body="x").status_code == 404
    assert (
        act(
            client, other, cid, "assign", Role.ADMIN, assignee_id=None, expected_version=1
        ).status_code
        == 404
    )
    # assigning to a user of another tenant is refused
    r = act(
        client,
        tenant,
        cid,
        "assign",
        Role.ADMIN,
        assignee_id=user_id(engine, other, "analyst"),
        expected_version=1,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_assignee"
    assert {
        u["email"]
        for u in client.get("/api/v1/cases/assignees", headers=tenant.headers(Role.VIEWER)).json()
    } == {"admin@acme.example.com", "analyst@acme.example.com"}


# ---------------------------------------------------------------- lifecycle


def test_lifecycle_assignment_history_and_audit(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    before = assessment_row(engine, a["assessment_id"])
    case = open_case(client, tenant, a["assessment_id"]).json()["case"]
    cid = case["id"]
    analyst = user_id(engine, tenant, "analyst")
    # an unassigned analyst cannot work the case
    r = act(client, tenant, cid, "status", Role.ANALYST, status="under_review", expected_version=1)
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_assignee"
    assert case["allowed_transitions"] == []
    c = act(
        client, tenant, cid, "assign", Role.ANALYST, assignee_id=analyst, expected_version=1
    ).json()
    assert c["assigned_to"]["id"] == analyst and c["version"] == 2
    assert c["allowed_transitions"] == ["escalated", "under_review"]
    # new cannot be resolved directly
    r = act(client, tenant, cid, "resolve", Role.ANALYST, outcome="fraud", expected_version=2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    c = act(
        client, tenant, cid, "status", Role.ANALYST, status="under_review", expected_version=2
    ).json()
    c = act(
        client, tenant, cid, "status", Role.ANALYST, status="escalated", expected_version=3
    ).json()
    assert c["resolved_at"] is None
    c = act(
        client,
        tenant,
        cid,
        "resolve",
        Role.ANALYST,
        outcome="legitimate",
        note="Customer confirmed the purchase",
        expected_version=4,
    ).json()
    assert c["status"] == "resolved_legitimate" and c["resolved_at"] is not None
    assert c["version"] == 5 and c["allowed_transitions"] == []
    # closed: analysts cannot reopen or reassign; admins can reopen
    r = act(client, tenant, cid, "status", Role.ANALYST, status="under_review", expected_version=5)
    assert r.status_code == 403
    r = act(client, tenant, cid, "assign", Role.ADMIN, assignee_id=None, expected_version=5)
    assert r.status_code == 409 and r.json()["error"]["code"] == "case_closed"
    c = act(
        client, tenant, cid, "status", Role.ADMIN, status="under_review", expected_version=5
    ).json()
    assert c["status"] == "under_review" and c["resolved_at"] is None and c["version"] == 6
    assert [
        (e["kind"], e["from_value"], e["to_value"], e["case_version"]) for e in c["events"]
    ] == [
        ("created", None, "new", 1),
        ("assigned", None, analyst, 2),
        ("status_changed", "new", "under_review", 3),
        ("status_changed", "under_review", "escalated", 4),
        ("resolved", "escalated", "resolved_legitimate", 5),
        ("reopened", "resolved_legitimate", "under_review", 6),
    ]
    assert [n["body"] for n in c["notes"]] == ["Customer confirmed the purchase"]
    with Session(engine) as s:
        actions = s.scalars(
            select(AuditLog.action).where(AuditLog.resource_id == cid).order_by(AuditLog.id)
        ).all()
    assert actions == [
        "case.create",
        "case.assign",
        "case.status",
        "case.status",
        "case.resolve",
        "case.reopen",
    ]
    # resolving as legitimate did not touch the assessment
    assert assessment_row(engine, a["assessment_id"]) == before


def test_admin_reassigns_and_analysts_cannot_take_others_cases(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    analyst = user_id(engine, tenant, "analyst")
    a2, token2 = second_analyst(engine, client, tenant)
    h2 = {"Authorization": f"Bearer {token2}"}
    act(client, tenant, cid, "assign", Role.ANALYST, assignee_id=analyst, expected_version=1)
    r = client.post(
        f"/api/v1/cases/{cid}/assign", json={"assignee_id": a2, "expected_version": 2}, headers=h2
    )
    assert r.status_code == 403  # analyst2 cannot take analyst1's case
    r = client.post(
        f"/api/v1/cases/{cid}/status",
        json={"status": "under_review", "expected_version": 2},
        headers=h2,
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "not_assignee"
    c = act(client, tenant, cid, "assign", Role.ADMIN, assignee_id=a2, expected_version=2).json()
    assert c["assigned_to"]["id"] == a2
    r = act(client, tenant, cid, "assign", Role.ADMIN, assignee_id=a2, expected_version=3)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_change"
    # any analyst may add notes (collaboration), assignment not required
    assert act(client, tenant, cid, "notes", Role.ANALYST, body="Second opinion").status_code == 201


# ---------------------------------------------------------------- optimistic locking


def test_stale_update_returns_409_without_overwriting(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    analyst = user_id(engine, tenant, "analyst")
    admin = user_id(engine, tenant, "admin")
    assert (
        act(
            client, tenant, cid, "assign", Role.ADMIN, assignee_id=analyst, expected_version=1
        ).status_code
        == 200
    )
    # a second admin tab still holds version 1
    r = act(client, tenant, cid, "assign", Role.ADMIN, assignee_id=admin, expected_version=1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    c = client.get(f"/api/v1/cases/{cid}", headers=tenant.headers(Role.VIEWER)).json()
    assert c["assigned_to"]["id"] == analyst and c["version"] == 2
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(CaseEvent)) == 2


def test_concurrent_transitions_only_one_wins(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    act(
        client,
        tenant,
        cid,
        "assign",
        Role.ADMIN,
        assignee_id=user_id(engine, tenant, "analyst"),
        expected_version=1,
    )
    targets = ["under_review", "escalated"] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        rs = list(
            pool.map(
                lambda t: act(
                    client, tenant, cid, "status", Role.ANALYST, status=t, expected_version=2
                ),
                targets,
            )
        )
    codes = sorted(r.status_code for r in rs)
    assert codes.count(200) == 1 and codes.count(409) == 7, [r.text for r in rs]
    assert {r.json()["error"]["code"] for r in rs if r.status_code == 409} == {"version_conflict"}
    with Session(engine) as s:
        assert s.scalar(select(Case.version).where(Case.id == uuid.UUID(cid))) == 3
        assert s.scalar(select(func.count()).select_from(CaseEvent)) == 3


def test_database_guards_cases(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    for sql, match in [
        ("UPDATE cases SET status = 'under_review'", "version must increase"),
        ("UPDATE cases SET version = version + 2", "version must increase"),
        ("UPDATE cases SET opened_at = now(), version = version + 1", "immutable"),
        ("DELETE FROM cases", "cannot be deleted"),
        ("UPDATE cases SET status = 'resolved_fraud', version = version + 1", "ck_cases_resolved"),
        ("UPDATE cases SET resolved_at = now(), version = version + 1", "ck_cases_resolved"),
        ("UPDATE case_events SET kind = 'created'", "append-only"),
        ("DELETE FROM case_events", "append-only"),
    ]:
        with engine.begin() as conn, pytest.raises(DBAPIError, match=match):
            conn.execute(text(sql))
    assert cid


# ---------------------------------------------------------------- notes


def test_notes_are_append_only_and_validated(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    r = act(
        client,
        tenant,
        cid,
        "notes",
        Role.ANALYST,
        body="Called the customer",
        evidence=["ticket:123"],
    )
    assert r.status_code == 201 and r.json()["evidence"] == ["ticket:123"]
    assert act(client, tenant, cid, "notes", Role.ANALYST, body="").status_code == 422
    assert act(client, tenant, cid, "notes", Role.ANALYST, body="x" * 10001).status_code == 422
    assert act(client, tenant, cid, "notes", Role.ANALYST, body="ok", extra=1).status_code == 422
    for sql in ("UPDATE case_notes SET body = 'rewritten'", "DELETE FROM case_notes"):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(sql))
    with Session(engine) as s:
        assert s.scalars(select(CaseNote.body)).all() == ["Called the customer"]
        assert (
            s.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "case.note")
            )
            == 1
        )


# ---------------------------------------------------------------- queue


def test_queue_filters_and_sorting(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    ids = {}
    for i, amount in enumerate(["5.00", "300.00", "950.00"]):
        a = scored(
            client,
            tenant,
            f"q{i}",
            amount=amount,
            customer_id=f"cus_{i:016x}",
            source=["replay", "simulated", "live"][i],
        )
        ids[amount] = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    act(
        client,
        tenant,
        ids["950.00"],
        "assign",
        Role.ANALYST,
        assignee_id=user_id(engine, tenant, "analyst"),
        expected_version=1,
    )
    h = tenant.headers(Role.ANALYST)

    def q(params: str) -> list[str]:
        return [
            c["transaction_id"]
            for c in client.get(f"/api/v1/cases?{params}", headers=h).json()["items"]
        ]

    assert q("sort=severity&order=desc") == ["q2", "q1", "q0"]
    assert q("assigned=me") == ["q2"]
    assert sorted(q("assigned=unassigned")) == ["q0", "q1"]
    assert q("severity=low") == ["q0"]
    assert q("source=simulated") == ["q1"]
    assert q("status=under_review") == []
    assert client.get("/api/v1/cases?status=closed", headers=h).status_code == 422


# ---------------------------------------------------------------- database failure


def test_database_failure_changes_nothing(
    client: TestClient, tenant: Tenant, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = scored(client, tenant)

    def broken_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    monkeypatch.setattr(Session, "commit", broken_commit)
    r = open_case(client, tenant, a["assessment_id"])
    monkeypatch.undo()
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"
    assert r.headers["Retry-After"] == "5"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Case)) == 0
        assert (
            s.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "case.create")
            )
            == 0
        )
    assert open_case(client, tenant, a["assessment_id"]).status_code == 201  # retry works
