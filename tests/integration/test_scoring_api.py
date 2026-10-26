from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from fraud_api.core.config import FailureMode
from fraud_api.core.security import Role
from fraud_api.db.models import AuditLog, RiskAssessment, Transaction
from fraud_ml.features.online import HistoryEvent, compute_online_features, to_epoch_us
from fraud_ml.features.spec import NUMERIC_FEATURES, encode
from fraud_ml.models.artifact import load_artifact
from tests.integration.conftest import AppFactory, Tenant, create_tenant

pytestmark = pytest.mark.integration

T0 = datetime(2020, 3, 15, 14, 30, tzinfo=UTC)


def payload(txn_id: str = "txn_1", **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transaction_id": txn_id,
        "customer_id": "cus_00000000000000aa",
        "merchant_id": "Acme",
        "amount": "125.40",
        "currency": "USD",
        "category": "shopping_net",
        "event_time": T0.isoformat(),
        "source": "replay",
    }
    body.update(overrides)
    return body


def score(
    client: TestClient, tenant: Tenant, body: dict[str, Any], role: Role = Role.SERVICE
) -> Any:
    return client.post("/api/v1/fraud/score", json=body, headers=tenant.headers(role))


# ---------------------------------------------------------------- health / auth / RBAC


def test_health(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"]["database"] == "ok"


def test_score_requires_token(client: TestClient) -> None:
    r = client.post("/api/v1/fraud/score", json=payload())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert r.headers["x-request-id"]


def test_forged_token_rejected(client: TestClient, tenant: Tenant) -> None:
    header, body, _sig = tenant.tokens[Role.SERVICE].split(".")
    r = client.post(
        "/api/v1/fraud/score",
        json=payload(),
        headers={"Authorization": f"Bearer {header}.{body}.forged"},
    )
    assert r.status_code == 401


def test_unsigned_alg_none_token_rejected(client: TestClient, tenant: Tenant) -> None:
    import jwt as pyjwt

    claims = pyjwt.decode(tenant.tokens[Role.ADMIN], options={"verify_signature": False})
    forged = pyjwt.encode(claims, key="", algorithm="none")
    r = client.post(
        "/api/v1/fraud/score", json=payload(), headers={"Authorization": f"Bearer {forged}"}
    )
    assert r.status_code == 401


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ANALYST])
def test_read_only_roles_cannot_score(client: TestClient, tenant: Tenant, role: Role) -> None:
    r = score(client, tenant, payload(), role=role)
    assert r.status_code == 403


def test_bad_login(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    r = client.post(
        "/api/v1/auth/token",
        json={"email": "service@acme.example.com", "password": "wrong-password-123"},
    )
    assert r.status_code == 401
    r = client.post(
        "/api/v1/auth/token",
        json={"email": "nobody@acme.example.com", "password": "wrong-password-123"},
    )
    assert r.status_code == 401
    with Session(engine) as s:
        assert s.scalar(select(func.count()).where(AuditLog.action == "auth.login_failed")) == 2


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"event_time": "2020-03-15T14:30:00"}, "event_time"),  # no timezone
        ({"category": "crypto"}, "category"),
        ({"amount": "10.123"}, "amount"),
        ({"amount": "-5"}, "amount"),
        ({"currency": "EUR"}, "currency"),
        ({"transaction_id": "bad id with spaces"}, "transaction_id"),
        ({"cvv": "987"}, "cvv"),  # unknown fields are rejected, never stored
    ],
)
def test_validation_errors(
    client: TestClient, tenant: Tenant, overrides: dict[str, Any], field: str
) -> None:
    r = score(client, tenant, payload(**overrides))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert any(field in d["loc"] for d in err["details"])
    # Submitted values are not echoed back.
    assert all("input" not in d for d in err["details"])
    for value in overrides.values():
        assert str(value) not in str(err["details"])


# ---------------------------------------------------------------- scoring correctness


def test_score_uses_real_model_and_point_in_time_history(
    client: TestClient, tenant: Tenant, engine: Engine, trained_artifact: Path
) -> None:
    history = [
        (T0 - timedelta(days=40), "50.00", "grocery_pos"),  # outside 30d lookback
        (T0 - timedelta(days=3), "20.00", "shopping_net"),
        (T0 - timedelta(minutes=30), "35.10", "shopping_net"),
        (T0, "99.00", "home"),  # same timestamp: not history
        (T0 + timedelta(minutes=5), "999.00", "travel"),  # future: not history
    ]
    for i, (t, amount, cat) in enumerate(history):
        r = score(
            client, tenant, payload(f"h{i}", event_time=t.isoformat(), amount=amount, category=cat)
        )
        assert r.status_code == 200, r.text

    r = score(client, tenant, payload("txn_main"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "scored"
    assert body["decision_mode"] == "sandbox"
    assert body["source"] == "replay"
    assert body["model_name"] == "test-model"

    # Recompute independently from the spec and the artifact.
    expected_history = [HistoryEvent(to_epoch_us(t), float(a), c) for t, a, c in history[1:3]]
    feats = compute_online_features(
        T0, 125.40, "shopping_net", "card_not_present", expected_history
    )
    x = encode(
        {n: [float(feats[n])] for n in NUMERIC_FEATURES},
        {"category": ["shopping_net"], "channel": ["card_not_present"]},
    )
    expected_score = float(load_artifact(trained_artifact).predict_proba(x)[0])
    assert body["risk_score"] == pytest.approx(expected_score, rel=1e-9)

    detail = client.get(
        f"/api/v1/fraud/predictions/{body['assessment_id']}", headers=tenant.headers(Role.ANALYST)
    ).json()
    assert detail["features"]["cust_txn_count_30d"] == 2
    assert detail["features"]["cust_txn_count_1h"] == 1
    assert detail["features"]["cust_same_category_count_30d"] == 2
    assert detail["features"]["seconds_since_prev_txn"] == 1800

    with Session(engine) as s:
        txn = s.scalar(select(Transaction).where(Transaction.external_id == "txn_main"))
        assert txn is not None and txn.amount == Decimal("125.40")


def test_first_transaction_has_null_history_features(client: TestClient, tenant: Tenant) -> None:
    body = score(client, tenant, payload()).json()
    detail = client.get(
        f"/api/v1/fraud/predictions/{body['assessment_id']}", headers=tenant.headers(Role.VIEWER)
    ).json()
    assert detail["features"]["seconds_since_prev_txn"] is None
    assert detail["features"]["cust_txn_count_30d"] == 0
    assert 0.0 <= body["risk_score"] <= 1.0


# ---------------------------------------------------------------- idempotency


def test_idempotent_retry_returns_original(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    first = score(client, tenant, payload()).json()
    second = score(client, tenant, payload()).json()
    assert second["assessment_id"] == first["assessment_id"]
    assert second["risk_score"] == first["risk_score"]
    assert (first["idempotent_replay"], second["idempotent_replay"]) == (False, True)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 1


def test_idempotency_conflict(client: TestClient, tenant: Tenant) -> None:
    assert score(client, tenant, payload()).status_code == 200
    r = score(client, tenant, payload(amount="999.99"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_concurrent_duplicates_produce_one_assessment(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: score(client, tenant, payload("dup")), range(8)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()["assessment_id"] for r in responses}) == 1
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(RiskAssessment)) == 1


# ---------------------------------------------------------------- tenant isolation


def test_cross_organization_isolation(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    other = create_tenant(engine, client, "globex")
    # Same customer id and transaction id in another org: independent namespace.
    for i in range(5):
        score(
            client, other, payload(f"o{i}", event_time=(T0 - timedelta(minutes=i + 1)).isoformat())
        )
    mine = score(client, tenant, payload("o0")).json()
    assert mine["idempotent_replay"] is False

    detail = client.get(
        f"/api/v1/fraud/predictions/{mine['assessment_id']}", headers=tenant.headers(Role.ANALYST)
    ).json()
    assert detail["features"]["cust_txn_count_1h"] == 0  # other org's history not visible

    r = client.get(
        f"/api/v1/fraud/predictions/{mine['assessment_id']}", headers=other.headers(Role.ADMIN)
    )
    assert r.status_code == 404


# ---------------------------------------------------------------- failure handling


def test_missing_model_degrades_to_manual_review(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    assert client.get("/health/ready").status_code == 503
    body = score(client, tenant, payload()).json()
    assert body["status"] == "degraded"
    assert body["risk_score"] is None
    assert body["risk_level"] == "unknown"
    assert body["decision"] == "review"  # never silently allowed
    assert body["reasons"] == ["scoring_failed: model_unavailable"]


def test_reject_mode_returns_503_and_persists_nothing(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing", failure_mode=FailureMode.REJECT)
    tenant = create_tenant(engine, client, "acme")
    r = score(client, tenant, payload())
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == 0


def test_history_limit_is_an_error_not_truncation(app_factory: AppFactory, engine: Engine) -> None:
    client = app_factory(max_history_rows=100)
    tenant = create_tenant(engine, client, "acme")
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO transactions (id, organization_id, external_id, customer_id, amount,
                currency, category, channel, event_time, source, schema_version, payload_sha256)
            SELECT gen_random_uuid(), :org, 'bf' || g, 'cus_00000000000000aa', 10, 'USD', 'home',
                'unspecified', :t - (g || ' seconds')::interval, 'backfill', '1.0', 'x'
            FROM generate_series(1, 150) g
        """),
            {"org": tenant.org_id, "t": T0},
        )
    body = score(client, tenant, payload()).json()
    assert body["status"] == "degraded"
    assert "exceeds 100 rows" in body["reasons"][0]


def test_policy_bound_to_other_model_fails_startup(
    app_factory: AppFactory, tmp_path: Path, policy_path: Path
) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text(policy_path.read_text().replace("0.0.1", "9.9.9"))
    with pytest.raises(ValueError, match="bound to"):
        app_factory(policy_path=bad)


# ---------------------------------------------------------------- audit


def test_scoring_is_audited_and_audit_log_is_append_only(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    body = score(client, tenant, payload()).json()
    with Session(engine) as s:
        entry = s.scalar(select(AuditLog).where(AuditLog.action == "fraud.score"))
        assert entry is not None and entry.resource_id == body["assessment_id"]
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("UPDATE audit_logs SET action = 'tampered'"))
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_logs"))


def test_degraded_allow_is_impossible_at_database_level(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    body = score(client, tenant, payload()).json()
    with pytest.raises(DBAPIError, match="ck_risk_assessments"), engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE risk_assessments SET status='degraded', risk_score=NULL, "
                "decision='allow' WHERE id = :id"
            ),
            {"id": body["assessment_id"]},
        )
    assert not math.isnan(body["processing_time_ms"])


# ---------------------------------------------------------------- model registry


def test_concurrent_model_registration_is_race_free(engine: Engine, trained_artifact: Path) -> None:
    from fraud_api.db.models import ModelVersion
    from fraud_api.main import register_model

    model = load_artifact(trained_artifact)

    def register(_: int) -> str:
        with Session(engine) as s:
            return str(register_model(s, model))

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = set(pool.map(register, range(6)))
    assert len(ids) == 1
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(ModelVersion)) == 1


def test_reusing_model_version_with_different_bytes_is_refused(
    engine: Engine, trained_artifact: Path
) -> None:
    from fraud_api.main import register_model
    from fraud_ml.models.artifact import ArtifactError

    model = load_artifact(trained_artifact)
    with Session(engine) as s:
        register_model(s, model)
    tampered = type(model)(
        booster=model.booster, manifest={**model.manifest, "model_sha256": "f" * 64}
    )
    with Session(engine) as s, pytest.raises(ArtifactError, match="different artifact hash"):
        register_model(s, tampered)
