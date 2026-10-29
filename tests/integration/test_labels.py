"""Phase 8: fraud labels, provenance, maturity and case/label analytics (PostgreSQL)."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from fraud_api.core.security import Role
from fraud_api.db.models import AuditLog, FraudLabel, Transaction, User
from fraud_api.labels.service import import_dataset_labels
from tests.integration.conftest import Tenant, create_tenant
from tests.integration.test_analytics import _assess, _txn
from tests.integration.test_cases import act, assessment_row, open_case, scored, user_id

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def label(client: TestClient, tenant: Tenant, role: Role = Role.ANALYST, **body: Any) -> Any:
    base = {
        "transaction_id": "t1",
        "label": "fraud",
        "source": "chargeback",
        "confidence": "confirmed",
        "labeled_at": NOW.isoformat(),
    }
    return client.post("/api/v1/labels", json=base | body, headers=tenant.headers(role))


def history(client: TestClient, tenant: Tenant, txn: str = "t1") -> Any:
    return client.get(
        f"/api/v1/labels?transaction_id={txn}", headers=tenant.headers(Role.VIEWER)
    ).json()


# ---------------------------------------------------------------- append-only supersession


def test_labels_are_append_only_with_explicit_supersession(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    before = assessment_row(engine, a["assessment_id"])
    first = label(client, tenant, label="fraud", source="chargeback")
    assert first.status_code == 201, first.text
    f = first.json()
    assert f["labeled_by"]["id"] == user_id(engine, tenant, "analyst")
    assert f["current"] is True and f["supersedes_id"] is None
    assert f["provenance"] == {"recorded_via": "api"}
    # a second label must supersede the current one explicitly
    r = label(client, tenant, label="legitimate", source="customer_report")
    assert r.status_code == 409 and r.json()["error"]["code"] == "label_conflict"
    r = label(client, tenant, label="legitimate", supersedes_id=f["id"] + 999, reason="x")
    assert r.status_code == 409
    r = label(client, tenant, label="legitimate", supersedes_id=f["id"])
    assert r.status_code == 422  # a correction needs a reason
    corr = label(
        client,
        tenant,
        label="legitimate",
        source="customer_report",
        supersedes_id=f["id"],
        reason="Customer proved the purchase",
    )
    assert corr.status_code == 201, corr.text
    h = history(client, tenant)
    assert [(x["label"], x["current"], x["superseded_by"]) for x in h["history"]] == [
        ("fraud", False, corr.json()["id"]),
        ("legitimate", True, None),
    ]
    assert h["current"]["id"] == corr.json()["id"]
    for sql in ("UPDATE fraud_labels SET label = 'fraud'", "DELETE FROM fraud_labels"):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="append-only"):
            conn.execute(text(sql))
    with Session(engine) as s:
        actions = s.scalars(
            select(AuditLog.action).where(AuditLog.action.like("label.%")).order_by(AuditLog.id)
        ).all()
    assert actions == ["label.create", "label.supersede"]
    # labels never change the assessment
    assert assessment_row(engine, a["assessment_id"]) == before


def test_concurrent_corrections_cannot_fork_history(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    scored(client, tenant)
    head = label(client, tenant).json()["id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rs = list(
            pool.map(
                lambda i: label(
                    client, tenant, label="legitimate", supersedes_id=head, reason=f"correction {i}"
                ),
                range(6),
            )
        )
    assert sorted(r.status_code for r in rs) == [201, 409, 409, 409, 409, 409], [r.text for r in rs]
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(FraudLabel)) == 2
    # two concurrent *first* labels: also exactly one wins
    scored(client, tenant, "t2", event_time="2020-03-15T15:00:00Z")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rs = list(pool.map(lambda _: label(client, tenant, transaction_id="t2"), range(4)))
    assert sorted(r.status_code for r in rs) == [201, 409, 409, 409]


def test_label_validation_and_provenance_rules(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    a = scored(client, tenant)
    scored(client, tenant, "t2", event_time="2020-03-15T15:00:00Z")
    future = (NOW + timedelta(hours=1)).isoformat()
    assert label(client, tenant, labeled_at=future).status_code == 422
    assert label(client, tenant, labeled_at="2020-03-15T14:00:00Z").status_code == 422  # pre-event
    assert label(client, tenant, labeled_at="2020-03-15T14:30:00").status_code == 422  # naive
    r = label(client, tenant, source="analyst_review")
    assert r.status_code == 422 and "case" in r.json()["error"]["message"]
    assert label(client, tenant, source="dataset").status_code == 422  # CLI only
    assert label(client, tenant, label="maybe").status_code == 422
    assert label(client, tenant, labeled_by=str(uuid.uuid4())).status_code == 422  # from token
    assert label(client, tenant, transaction_id="nope").status_code == 404
    cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
    r = label(client, tenant, transaction_id="t2", source="analyst_review", case_id=cid)
    assert r.status_code == 422 and "different transaction" in r.json()["error"]["message"]
    ok = label(client, tenant, source="analyst_review", case_id=cid, confidence="probable")
    assert ok.status_code == 201 and ok.json()["case_id"] == cid
    for role in (Role.VIEWER, Role.SERVICE):
        assert label(client, tenant, role, transaction_id="t2").status_code == 403
    assert (
        client.get(
            "/api/v1/labels?transaction_id=t1", headers=tenant.headers(Role.SERVICE)
        ).status_code
        == 403
    )
    # tenant isolation
    other = create_tenant(engine, client, "globex")
    assert label(client, other, Role.ADMIN).status_code == 404
    assert (
        client.get(
            "/api/v1/labels?transaction_id=t1", headers=other.headers(Role.VIEWER)
        ).status_code
        == 404
    )
    with engine.begin() as conn, pytest.raises(DBAPIError, match="ck_fraud_labels_labeled_by"):
        conn.execute(
            text(
                "INSERT INTO fraud_labels (organization_id, transaction_id, label, source,"
                " confidence, labeled_at, provenance) SELECT organization_id, id, 'fraud',"
                " 'chargeback', 'confirmed', now(), '{}' FROM transactions WHERE external_id = 't2'"
            )
        )


def test_resolve_with_label_and_refusal_when_labeled(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    analyst = user_id(engine, tenant, "analyst")
    for txn, amount in (("t1", "950.00"), ("t2", "5.00")):
        a = scored(client, tenant, txn, amount=amount, customer_id=f"cus_{txn:0>16}")
        cid = open_case(client, tenant, a["assessment_id"]).json()["case"]["id"]
        act(client, tenant, cid, "assign", Role.ANALYST, assignee_id=analyst, expected_version=1)
        act(client, tenant, cid, "status", Role.ANALYST, status="under_review", expected_version=2)
        if txn == "t1":
            c = act(
                client,
                tenant,
                cid,
                "resolve",
                Role.ANALYST,
                outcome="fraud",
                record_label=True,
                label_confidence="probable",
                expected_version=3,
            ).json()
            cur = c["labels"]["current"]
            assert (cur["label"], cur["source"], cur["confidence"], cur["case_id"]) == (
                "fraud",
                "analyst_review",
                "probable",
                cid,
            )
            assert datetime.fromisoformat(cur["labeled_at"]) == datetime.fromisoformat(
                c["resolved_at"]
            )
            assert c["current_label"] == "fraud"
        else:
            label(client, tenant, transaction_id="t2", label="legitimate")
            r = act(
                client,
                tenant,
                cid,
                "resolve",
                Role.ANALYST,
                outcome="fraud",
                record_label=True,
                expected_version=3,
            )
            assert r.status_code == 409 and r.json()["error"]["code"] == "label_exists"
            c = client.get(f"/api/v1/cases/{cid}", headers=tenant.headers(Role.VIEWER)).json()
            assert c["status"] == "under_review" and c["version"] == 3  # nothing changed
            assert len(c["labels"]["history"]) == 1


# ---------------------------------------------------------------- dataset labels


def test_dataset_label_import_is_idempotent_and_never_overwrites(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    scored(client, tenant)
    scored(client, tenant, "t2", event_time="2020-03-15T15:00:00Z")
    label(client, tenant, transaction_id="t2", label="legitimate")
    df = pd.DataFrame({"event_id": ["t1", "t2", "missing"], "is_fraud": [1, 1, 0]})
    with Session(engine) as s:
        r1 = import_dataset_labels(s, tenant.org_id, df, {"dataset_version": "v-test"})
        r2 = import_dataset_labels(s, tenant.org_id, df, {"dataset_version": "v-test"})
    assert r1 == {"created": 1, "already_labeled": 1, "not_in_database": 1}
    assert r2 == {"created": 0, "already_labeled": 2, "not_in_database": 1}
    h1, h2 = history(client, tenant, "t1"), history(client, tenant, "t2")
    assert h1["current"]["source"] == "dataset" and h1["current"]["labeled_by"] is None
    assert h1["current"]["provenance"]["column"] == "is_fraud"
    assert h1["current"]["provenance"]["dataset_version"] == "v-test"
    assert h2["current"]["source"] == "chargeback"  # analyst/chargeback labels untouched


# ---------------------------------------------------------------- maturity and performance

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
EV = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def cohort(client: TestClient, tenant: Tenant, engine: Engine) -> dict[str, Any]:
    """Known cohort (event window [2026-07-01, 2026-10-01), as_of 2026-10-01, maturity 30 d)."""
    with Session(engine) as s:
        admin = s.scalar(
            select(User.id).where(User.organization_id == tenant.org_id, User.role == "admin")
        )
        txns = {}

        def row(ext: str, event: datetime, decision: str, policy: str, score: float) -> None:
            t = _txn(s, tenant.org_id, ext, "replay", event, event)
            _assess(
                s, t, event, decision, policy, "high", "evaluated", {"total": 1.0}, status="scored"
            )
            s.execute(
                text("UPDATE risk_assessments SET risk_score = :r WHERE transaction_id = :t"),
                {"r": score, "t": t.id},
            )
            txns[ext] = t

        row("tp", EV, "decline", "review", 0.9)
        row("fn", EV, "allow", "allow", 0.05)
        row("fp", EV, "review", "allow", 0.3)  # rule-escalated
        row("tn", EV, "allow", "allow", 0.01)
        row("prob", EV, "decline", "decline", 0.99)
        row("inc", EV, "review", "review", 0.5)
        row("unl", EV, "allow", "allow", 0.02)
        row("late", EV, "decline", "decline", 0.97)
        row("corr", EV, "allow", "allow", 0.04)
        row("imm", datetime(2026, 9, 15, tzinfo=UTC), "allow", "allow", 0.03)

        def lab(
            ext: str,
            value: str,
            when: datetime,
            conf: str = "confirmed",
            supersedes: int | None = None,
        ) -> int:
            fl = FraudLabel(
                organization_id=tenant.org_id,
                transaction_id=txns[ext].id,
                label=value,
                source="chargeback",
                confidence=conf,
                labeled_by=admin,
                labeled_at=when,
                supersedes_id=supersedes,
                reason="fixture" if supersedes else None,
                provenance={},
            )
            s.add(fl)
            s.flush()
            return fl.id

        known = datetime(2026, 9, 20, tzinfo=UTC)
        lab("tp", "fraud", known)
        lab("fn", "fraud", known)
        lab("fp", "legitimate", known)
        lab("tn", "legitimate", known)
        lab("prob", "fraud", known, conf="probable")
        lab("inc", "inconclusive", known)
        lab("late", "fraud", datetime(2026, 10, 10, tzinfo=UTC))  # known only after as_of
        first = lab("corr", "legitimate", known)
        lab("corr", "fraud", datetime(2026, 10, 12, tzinfo=UTC), supersedes=first)
        lab("imm", "fraud", known)
        s.commit()
    return txns


def perf(client: TestClient, tenant: Tenant, **params: Any) -> Any:
    q = {
        "start": "2026-07-01T00:00:00Z",
        "end": "2026-10-01T00:00:00Z",
        "as_of": AS_OF.isoformat(),
        "maturity_days": 30,
    } | params
    r = client.get(
        "/api/v1/fraud/insights/performance", params=q, headers=tenant.headers(Role.VIEWER)
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_label_matured_performance_excludes_immature_unlabeled_inconclusive_probable(
    client: TestClient, tenant: Tenant, cohort: dict[str, Any]
) -> None:
    p = perf(client, tenant)
    assert p["maturity_cutoff"].startswith("2026-09-01")
    assert p["population"] == {
        "assessments_in_window": 10,
        "immature": 1,
        "mature": 9,
        "mature_unlabeled": 2,  # 'unl' and 'late' (label known only after as_of)
        "mature_inconclusive": 1,
        "mature_probable_excluded": 1,
        "usable": 5,
        "usable_fraud": 2,
        "usable_legitimate": 3,  # includes 'corr', still legitimate at as_of
        "label_coverage": {"numerator": 5, "denominator": 9, "rate": 5 / 9},
        "usable_by_source": {"chargeback": 5},
    }
    f, pol = p["final_decision"], p["policy_decision"]
    assert (f["true_positive"], f["false_positive"], f["false_negative"], f["true_negative"]) == (
        1,
        1,
        1,
        2,
    )
    assert (
        pol["true_positive"],
        pol["false_positive"],
        pol["false_negative"],
        pol["true_negative"],
    ) == (1, 0, 1, 3)
    assert f["precision"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert p["mean_score_fraud"] == pytest.approx((0.9 + 0.05) / 2)
    assert p["unlabeled_signals"]["assessments"] == 10
    assert p["unlabeled_signals"]["alert_rate"]["numerator"] == 5
    assert "not unbiased ground truth" in p["caveat"]


def test_probable_only_when_requested_and_point_in_time_labels(
    client: TestClient, tenant: Tenant, cohort: dict[str, Any]
) -> None:
    p = perf(client, tenant, include_probable="true")
    assert p["population"]["usable"] == 6 and p["final_decision"]["true_positive"] == 2
    # Later as_of: 'late' and the correction of 'corr' are now known, 'imm' has matured.
    later = perf(client, tenant, as_of="2026-10-20T00:00:00Z")
    assert later["population"]["immature"] == 0
    assert later["population"]["mature_unlabeled"] == 1
    f = later["final_decision"]
    # late: decline + fraud -> TP; corr: allow + fraud -> FN; imm: allow + fraud -> FN
    assert (f["true_positive"], f["false_negative"], f["true_negative"]) == (2, 3, 1)
    # as_of before any label: nothing usable, rates null, nothing treated as legitimate
    none = perf(client, tenant, as_of="2026-09-10T00:00:00Z")
    assert none["population"]["usable"] == 0
    assert none["final_decision"]["precision"]["rate"] is None
    assert none["population"]["mature_unlabeled"] == none["population"]["mature"]


def test_case_and_label_stats(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    analyst = user_id(engine, tenant, "analyst")
    ids = []
    for i in range(3):
        a = scored(
            client,
            tenant,
            f"c{i}",
            customer_id=f"cus_{i:016x}",
            amount=["5.00", "300.00", "950.00"][i],
        )
        ids.append(open_case(client, tenant, a["assessment_id"]).json()["case"]["id"])
    act(client, tenant, ids[0], "assign", Role.ANALYST, assignee_id=analyst, expected_version=1)
    act(client, tenant, ids[0], "status", Role.ANALYST, status="under_review", expected_version=2)
    act(
        client,
        tenant,
        ids[0],
        "resolve",
        Role.ANALYST,
        outcome="legitimate",
        record_label=True,
        expected_version=3,
    )
    label(client, tenant, transaction_id="c2", label="fraud")
    now = datetime.now(UTC)
    w = {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
    }
    h = tenant.headers(Role.VIEWER)
    c = client.get("/api/v1/fraud/insights/cases", params=w, headers=h).json()
    assert c["opened"] == 3 and c["resolved"] == 1
    assert c["resolved_by_outcome"]["resolved_legitimate"] == 1
    assert c["open_now_by_status"] == {"new": 2, "under_review": 0, "escalated": 0}
    assert c["open_now_unassigned"] == 2 and c["hours_to_resolution_p50"] is not None
    lab = client.get("/api/v1/fraud/insights/labels", params=w, headers=h).json()
    assert lab["recorded"] == 2 and lab["corrections"] == 0
    assert {(r["label"], r["source"], r["count"]) for r in lab["current"]} == {
        ("legitimate", "analyst_review", 1),
        ("fraud", "chargeback", 1),
    }
    assert "not automatic ground truth" in lab["note"]
    # other tenants and service accounts see nothing
    other = create_tenant(engine, client, "globex")
    assert (
        client.get(
            "/api/v1/fraud/insights/cases", params=w, headers=other.headers(Role.VIEWER)
        ).json()["opened"]
        == 0
    )
    assert (
        client.get(
            "/api/v1/fraud/insights/performance", params=w, headers=tenant.headers(Role.SERVICE)
        ).status_code
        == 403
    )
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 3
