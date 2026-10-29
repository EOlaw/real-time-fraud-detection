from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from fraud_api.core.security import Role
from tests.integration.conftest import AppFactory, Tenant, create_tenant
from tests.integration.test_scoring_api import T0, payload, score

pytestmark = pytest.mark.integration


def _seed(client: TestClient, tenant: Tenant, n: int = 5) -> list[dict[str, object]]:
    out = []
    for i in range(n):
        body = payload(
            f"txn_{i}",
            amount=f"{20 + 40 * i}.00",
            event_time=(T0 + timedelta(minutes=i)).isoformat(),
        )
        r = score(client, tenant, body)
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


def test_list_is_sorted_searchable_and_paginated(client: TestClient, tenant: Tenant) -> None:
    _seed(client, tenant)
    h = tenant.headers(Role.ANALYST)
    r = client.get("/api/v1/fraud/assessments?sort=amount&order=desc&limit=2", headers=h)
    assert r.status_code == 200
    page = r.json()
    assert page["total"] == 5
    assert [i["amount"] for i in page["items"]] == [180.0, 140.0]
    r = client.get("/api/v1/fraud/assessments?q=txn_3", headers=h).json()
    assert [i["transaction_id"] for i in r["items"]] == ["txn_3"]
    # LIKE wildcards are literal: "_" must not match any character.
    assert client.get("/api/v1/fraud/assessments?q=txn%25", headers=h).json()["total"] == 0


def test_service_role_cannot_read_insights(client: TestClient, tenant: Tenant) -> None:
    r = client.get("/api/v1/fraud/assessments", headers=tenant.headers(Role.SERVICE))
    assert r.status_code == 403


def test_list_is_isolated_per_organization(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    _seed(client, tenant, n=2)
    other = create_tenant(engine, client, "globex")
    r = client.get("/api/v1/fraud/assessments", headers=other.headers(Role.ANALYST))
    assert r.json()["total"] == 0


def test_explanation_reconstructs_stored_score(client: TestClient, tenant: Tenant) -> None:
    scored = _seed(client, tenant, n=3)
    target = scored[-1]
    r = client.get(
        f"/api/v1/fraud/predictions/{target['assessment_id']}/explanation?top_k=4",
        headers=tenant.headers(Role.VIEWER),
    )
    assert r.status_code == 200, r.text
    e = r.json()
    assert len(e["contributions"]) == 4
    assert math.isclose(e["probability"], target["risk_score"], abs_tol=1e-5)
    total = sum(c["contribution_log_odds"] for c in e["contributions"])
    assert math.isclose(
        e["base_value_log_odds"] + total + e["other_features_log_odds"],
        e["margin_log_odds"],
        abs_tol=1e-3,
    )
    assert max(abs(c["normalized"]) for c in e["contributions"]) == pytest.approx(1.0)


def test_degraded_assessment_is_not_explainable(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    r = score(client, tenant, payload())
    assert r.json()["status"] == "degraded"
    r = client.get(
        f"/api/v1/fraud/predictions/{r.json()['assessment_id']}/explanation",
        headers=tenant.headers(Role.ANALYST),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "not_explainable"


def test_distributions_share_sums_and_thresholds(client: TestClient, tenant: Tenant) -> None:
    _seed(client, tenant, n=4)
    r = client.get("/api/v1/fraud/insights/distributions", headers=tenant.headers(Role.ANALYST))
    assert r.status_code == 200
    body = r.json()
    assert body["sample_size"] == 4
    assert body["thresholds"] == {"challenge_at": 0.1, "review_at": 0.5, "decline_at": 0.95}
    for d in body["distributions"]:
        assert len(d["bins"]) == len(d["shares"])
        assert math.isclose(sum(d["shares"]), 1.0)
    category = next(d for d in body["distributions"] if d["feature"] == "category")
    assert category["shares"][category["bins"].index("shopping_net")] == 1.0
