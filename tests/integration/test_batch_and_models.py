"""Phase 4: batch scoring and model metadata endpoints (PostgreSQL)."""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from fraud_api.core.config import FailureMode
from fraud_api.core.security import Role
from fraud_api.db.models import AuditLog, ModelVersion, RiskAssessment, Transaction
from fraud_api.registry.service import register_model
from fraud_api.scoring import batch as batch_module
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.models.artifact import load_artifact
from fraud_ml.training.train import run_training
from tests.conftest import TEST_MODEL_NAME, small_train_config
from tests.fixtures import synthetic_events
from tests.integration.conftest import PASSWORD, AppFactory, Tenant, create_tenant

pytestmark = pytest.mark.integration

T0 = datetime(2020, 3, 15, 14, 30, tzinfo=UTC)


def item(
    txn_id: str, minutes: float = 0, customer: str = "cus_00000000000000aa", **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transaction_id": txn_id,
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


def post_batch(
    client: TestClient, tenant: Tenant, items: list[Any], role: Role = Role.SERVICE
) -> Any:
    return client.post("/api/v1/fraud/batch", json={"items": items}, headers=tenant.headers(role))


def detail(client: TestClient, tenant: Tenant, assessment_id: str) -> dict[str, Any]:
    r = client.get(f"/api/v1/fraud/predictions/{assessment_id}", headers=tenant.headers())
    assert r.status_code == 200, r.text
    return r.json()


def count(engine: Engine, model: Any) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------- mixed outcomes


def test_mixed_batch_outcomes_are_explicit_per_item(
    client: TestClient, tenant: Tenant, engine: Engine
) -> None:
    # Pre-existing state: one scored transaction, and one id reused with a different payload.
    assert (
        client.post(
            "/api/v1/fraud/score", json=item("prior_same", -60), headers=tenant.headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/fraud/score", json=item("prior_diff", -50), headers=tenant.headers()
        ).status_code
        == 200
    )

    items = [
        item("ok_1", 1),  # 0 scored
        item("bad_time", 2, event_time="2020-03-15T14:32:00"),  # 1 invalid (naive time)
        item("bad_cat", 3, category="crypto"),  # 2 invalid
        item("ok_1", 1),  # 3 duplicate of 0
        item("ok_1", 1, amount="999.99"),  # 4 conflict with 0
        item("prior_same", -60),  # 5 idempotent replay
        item("prior_diff", -50, amount="1.00"),  # 6 conflict with stored
        "not an object",  # 7 invalid
        item("ok_2", 4, cvv="123"),  # 8 invalid (extra field)
    ]
    r = post_batch(client, tenant, items)
    assert r.status_code == 200, r.text
    body = r.json()
    statuses = [i["status"] for i in body["items"]]
    assert statuses == [
        "scored",
        "invalid",
        "invalid",
        "duplicate",
        "conflict",
        "scored",
        "conflict",
        "invalid",
        "invalid",
    ]
    assert [i["index"] for i in body["items"]] == list(range(len(items)))
    assert body["complete"] is True and body["received"] == len(items)
    assert body["summary"]["scored"] == 2 and body["summary"]["invalid"] == 4
    assert body["summary"]["duplicate"] == 1 and body["summary"]["conflict"] == 2

    by = body["items"]
    assert by[3]["duplicate_of"] == 0
    assert by[3]["result"]["assessment_id"] == by[0]["result"]["assessment_id"]
    assert by[4]["error"]["code"] == "duplicate_id_in_batch" and by[4]["duplicate_of"] == 0
    assert by[5]["result"]["idempotent_replay"] is True
    assert by[6]["error"]["code"] == "idempotency_conflict"
    assert by[1]["error"]["details"][0]["loc"] == ["event_time"]
    assert "123" not in json.dumps(by[8]["error"])  # submitted values are not echoed
    for it in by:
        if it["status"] not in ("scored", "degraded", "duplicate"):
            assert it["result"] is None  # no decision at all, so never "allow"

    with Session(engine) as s:
        txns = set(s.scalars(select(Transaction.external_id)).all())
        audit = s.scalars(select(AuditLog).where(AuditLog.action == "fraud.batch_score")).one()
    assert txns == {"prior_same", "prior_diff", "ok_1"}
    assert audit.details["received"] == len(items) and audit.details["scored"] == 2


def test_batch_envelope_limits(app_factory: AppFactory, engine: Engine) -> None:
    client = app_factory(max_batch_size=3)
    tenant = create_tenant(engine, client, "acme")
    r = post_batch(client, tenant, [item(f"t{i}", i) for i in range(4)])
    assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"
    assert post_batch(client, tenant, []).status_code == 422
    r = client.post("/api/v1/fraud/batch", json={"items": "x"}, headers=tenant.headers())
    assert r.status_code == 422
    assert count(engine, Transaction) == 0


# ---------------------------------------------------------------- ordering and equivalence


def test_same_customer_events_are_processed_in_time_order(
    client: TestClient, tenant: Tenant
) -> None:
    items = [item(f"ev{k}", minutes=k) for k in range(5)]
    items.append(item("tie_a", minutes=2.0))  # same timestamp as ev2: neither sees the other
    random.Random(7).shuffle(items)
    body = post_batch(client, tenant, items).json()
    counts = {
        i["transaction_id"]: detail(client, tenant, i["result"]["assessment_id"])["features"][
            "cust_txn_count_1h"
        ]
        for i in body["items"]
    }
    assert counts == {"ev0": 0, "ev1": 1, "ev2": 2, "tie_a": 2, "ev3": 4, "ev4": 5}


def test_batch_matches_equivalent_sequential_requests(client: TestClient, engine: Engine) -> None:
    """Same events, shuffled into one batch for org A and sent one by one in time order for org B:
    identical features, scores and decisions."""
    a, b = create_tenant(engine, client, "batchorg"), create_tenant(engine, client, "seqorg")
    ev = synthetic_events(n=120, n_customers=6, seed=5)
    events = [
        item(
            r.event_id,
            customer=r.customer_id,
            amount=f"{r.amount:.2f}",
            category=r.category,
            event_time=r.event_time.isoformat(),
        )
        for r in ev.itertuples()
    ]
    shuffled = events.copy()
    random.Random(1).shuffle(shuffled)
    batch = post_batch(client, a, shuffled).json()
    assert batch["summary"]["scored"] == len(events)
    batch_by_id = {i["transaction_id"]: i["result"] for i in batch["items"]}

    for e in sorted(events, key=lambda e: (e["event_time"], e["transaction_id"])):
        r = client.post("/api/v1/fraud/score", json=e, headers=b.headers())
        assert r.status_code == 200
        seq = r.json()
        got = batch_by_id[e["transaction_id"]]
        assert (got["risk_score"], got["decision"], got["risk_level"]) == (
            seq["risk_score"],
            seq["decision"],
            seq["risk_level"],
        )
        fa = detail(client, a, got["assessment_id"])["features"]
        fb = detail(client, b, seq["assessment_id"])["features"]
        assert fa == fb


# ---------------------------------------------------------------- retries and concurrency


def test_retrying_a_batch_is_idempotent(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    items = [item(f"r{k}", k) for k in range(6)]
    first = post_batch(client, tenant, items).json()
    second = post_batch(client, tenant, items).json()
    assert [i["result"]["assessment_id"] for i in first["items"]] == [
        i["result"]["assessment_id"] for i in second["items"]
    ]
    assert all(i["result"]["idempotent_replay"] for i in second["items"])
    assert not any(i["result"]["idempotent_replay"] for i in first["items"])
    assert count(engine, Transaction) == 6 and count(engine, RiskAssessment) == 6


def test_concurrent_duplicate_batches(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    items = [item(f"c{k}", k) for k in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        bodies = [r.json() for r in pool.map(lambda _: post_batch(client, tenant, items), range(4))]
    ids = {tuple(i["result"]["assessment_id"] for i in b["items"]) for b in bodies}
    assert len(ids) == 1  # every concurrent batch reports the same assessments
    assert all(i["status"] == "scored" for b in bodies for i in b["items"])
    assert count(engine, Transaction) == 8 and count(engine, RiskAssessment) == 8


# ---------------------------------------------------------------- tenant isolation and auth


def test_tenant_isolation(client: TestClient, tenant: Tenant, engine: Engine) -> None:
    other = create_tenant(engine, client, "globex")
    post_batch(client, other, [item(f"h{k}", k - 10) for k in range(5)])
    body = post_batch(client, tenant, [item("h0", -10), item("mine", 0)]).json()
    assert [i["status"] for i in body["items"]] == ["scored", "scored"]
    assert body["items"][0]["result"]["idempotent_replay"] is False  # independent namespace
    f = detail(client, tenant, body["items"][1]["result"]["assessment_id"])["features"]
    assert f["cust_txn_count_1h"] == 1  # only this org's h0, not globex's five events
    r = client.get(
        f"/api/v1/fraud/predictions/{body['items'][1]['result']['assessment_id']}",
        headers=other.headers(Role.ADMIN),
    )
    assert r.status_code == 404


@pytest.mark.parametrize("role", [Role.VIEWER, Role.ANALYST])
def test_read_only_roles_cannot_batch_score(client: TestClient, tenant: Tenant, role: Role) -> None:
    assert post_batch(client, tenant, [item("x")], role=role).status_code == 403


def test_batch_requires_authentication(client: TestClient) -> None:
    r = client.post("/api/v1/fraud/batch", json={"items": [item("x")]})
    assert r.status_code == 401


# ---------------------------------------------------------------- failure behaviour


def test_missing_model_degrades_every_item_to_review(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    body = post_batch(client, tenant, [item(f"d{k}", k) for k in range(3)]).json()
    assert {i["status"] for i in body["items"]} == {"degraded"}
    assert {i["result"]["decision"] for i in body["items"]} == {"review"}
    assert all(i["result"]["risk_score"] is None for i in body["items"])


def test_reject_mode_reports_errors_and_persists_nothing(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing", failure_mode=FailureMode.REJECT)
    tenant = create_tenant(engine, client, "acme")
    body = post_batch(client, tenant, [item(f"e{k}", k) for k in range(3)]).json()
    assert [i["status"] for i in body["items"]] == ["error"] * 3
    assert {i["error"]["code"] for i in body["items"]} == {"scoring_unavailable"}
    assert all(i["result"] is None for i in body["items"])
    assert count(engine, Transaction) == 0


def test_database_error_stops_batch_and_retry_completes_it(
    client: TestClient, tenant: Tenant, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = batch_module.score_transaction

    def flaky(session: Session, ctx: Any, org: Any, actor: Any, req: Any, cid: Any) -> Any:
        if req.transaction_id == "f2":
            raise OperationalError("SELECT 1", {}, Exception("connection lost"))
        return real(session, ctx, org, actor, req, cid)

    items = [item(f"f{k}", k) for k in range(5)]
    monkeypatch.setattr(batch_module, "score_transaction", flaky)
    body = post_batch(client, tenant, list(reversed(items))).json()
    status = {i["transaction_id"]: i["status"] for i in body["items"]}
    assert status == {
        "f0": "scored",
        "f1": "scored",
        "f2": "error",
        "f3": "not_processed",
        "f4": "not_processed",
    }
    assert body["complete"] is False
    assert {i["error"]["code"] for i in body["items"] if i["error"]} == {
        "database_error",
        "not_processed",
    }
    assert count(engine, Transaction) == 2  # earlier items committed, nothing partial

    monkeypatch.setattr(batch_module, "score_transaction", real)
    retry = post_batch(client, tenant, items).json()
    assert retry["complete"] is True
    replayed = {i["transaction_id"]: i["result"]["idempotent_replay"] for i in retry["items"]}
    assert replayed == {"f0": True, "f1": True, "f2": False, "f3": False, "f4": False}
    assert count(engine, Transaction) == 5


# ---------------------------------------------------------------- model metadata


@pytest.fixture(scope="module")
def candidate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A second version of the test model plus a policy bound to it."""
    out = tmp_path_factory.mktemp("candidate") / "model"
    cfg = small_train_config(model_version="0.1.0", seed=2)
    run_training(
        cfg,
        synthetic_events(n=6_000, n_customers=40, seed=11),
        out,
        {"dataset_version": "synthetic-fixture"},
        log=lambda *_: None,
    )
    policy = out.parent / "policy.yaml"
    policy.write_text(
        yaml.safe_dump(
            {
                "version": "test-policy-candidate",
                "domain": "card_payments",
                "mode": "sandbox",
                "model": {"name": TEST_MODEL_NAME, "version": "0.1.0"},
                "thresholds": {"challenge_at": 0.2, "review_at": 0.6, "decline_at": 0.97},
                "on_scoring_failure": "review",
            }
        )
    )
    return out, policy


def _register_candidate(engine: Engine, candidate: tuple[Path, Path]) -> str:
    with Session(engine) as s:
        return str(
            register_model(
                s, load_artifact(candidate[0]), DecisionPolicy.load(candidate[1]), activate=False
            )
        )


def test_models_distinguish_active_from_candidate(
    client: TestClient,
    tenant: Tenant,
    engine: Engine,
    trained_artifact: Path,
    candidate: tuple[Path, Path],
) -> None:
    cand_id = _register_candidate(engine, candidate)
    r = client.get("/api/v1/models", headers=tenant.headers(Role.VIEWER))
    assert r.status_code == 200, r.text
    body = r.json()
    versions = {v["version"]: v for v in body["versions"]}
    active, cand = versions["0.0.1"], versions["0.1.0"]
    assert body["serving"]["version"] == "0.0.1"
    assert (active["status"], active["serving"]) == ("active", True)
    assert (cand["status"], cand["serving"]) == ("registered", False)
    assert active["artifact_sha256"] == load_artifact(trained_artifact).sha256
    assert cand["artifact_sha256"] == load_artifact(candidate[0]).sha256
    assert active["feature_spec_version"] == cand["feature_spec_version"] == "card-v1"
    assert active["dataset_version"] == "synthetic-fixture"
    assert active["bound_policy"]["version"] == "test-policy-1"  # the policy the API loaded
    assert cand["bound_policy"]["version"] == "test-policy-candidate"
    assert cand["bound_policy"]["thresholds"]["review_at"] == 0.6
    assert active["last_activated_at"] is not None and cand["last_activated_at"] is None

    act = client.get("/api/v1/models/active", headers=tenant.headers(Role.ANALYST)).json()
    assert act["version"] == "0.0.1" and act["serving"] is True
    assert act["feature_columns"] == load_artifact(trained_artifact).manifest["feature_columns"]
    det = client.get(f"/api/v1/models/{cand_id}", headers=tenant.headers()).json()
    assert det["version"] == "0.1.0" and det["serving"] is False
    assert det["metrics"]["validation"]["average_precision"] > 0
    m = client.get(f"/api/v1/models/{cand_id}/metrics", headers=tenant.headers(Role.ADMIN))
    assert m.status_code == 200 and "test_summary" in m.json()

    # Listing and registering never promote: the serving model is unchanged.
    with Session(engine) as s:
        statuses = dict(s.execute(select(ModelVersion.version, ModelVersion.status)).all())
    assert statuses == {"0.0.1": "active", "0.1.0": "registered"}
    score = client.post(
        "/api/v1/fraud/score", json=item("after_register"), headers=tenant.headers()
    ).json()
    assert score["model_version"] == "0.0.1"


def test_model_metadata_exposes_no_paths_secrets_or_training_config(
    client: TestClient, tenant: Tenant, engine: Engine, candidate: tuple[Path, Path]
) -> None:
    cand_id = _register_candidate(engine, candidate)
    texts = [
        client.get(url, headers=tenant.headers()).text
        for url in (
            "/api/v1/models",
            "/api/v1/models/active",
            f"/api/v1/models/{cand_id}",
            f"/api/v1/models/{cand_id}/metrics",
        )
    ]
    blob = "\n".join(texts)
    for forbidden in (
        "/tmp",  # noqa: S108 - asserting this path never appears, not using it
        "/private",
        "/Users",
        "artifacts",
        "model.json",
        "training_config",
        "policy_out_dir",
        "baseline_artifact",
        "cus_",
        "jwt",
        "database_url",
    ):
        assert forbidden not in blob, forbidden


def test_model_endpoints_require_auth_and_return_404(client: TestClient, tenant: Tenant) -> None:
    assert client.get("/api/v1/models").status_code == 401
    assert client.get("/api/v1/models/active").status_code == 401
    r = client.get("/api/v1/models/00000000-0000-0000-0000-000000000000", headers=tenant.headers())
    assert r.status_code == 404
    assert client.post("/api/v1/models", headers=tenant.headers(Role.ADMIN)).status_code == 405


def test_no_model_loaded_reports_unavailable(
    app_factory: AppFactory, engine: Engine, tmp_path: Path
) -> None:
    client = app_factory(model_dir=tmp_path / "missing")
    tenant = create_tenant(engine, client, "acme")
    assert client.get("/api/v1/models/active", headers=tenant.headers()).status_code == 503
    assert client.get("/api/v1/models", headers=tenant.headers()).json()["serving"] is None


def test_activation_on_startup_demotes_previous_active(
    app_factory: AppFactory, engine: Engine, candidate: tuple[Path, Path]
) -> None:
    """An instance that *loads* the candidate (i.e. after a real promotion) activates it; the
    previously active version becomes 'registered'. Registration alone never does this."""
    first = app_factory()  # serves 0.0.1
    create_tenant(engine, first, "acme")
    second = app_factory(model_dir=candidate[0], policy_path=candidate[1])
    # Each test app has its own JWT secret, so authenticate against the second instance.
    token = second.post(
        "/api/v1/auth/token", json={"email": "viewer@acme.example.com", "password": PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    versions = {
        v["version"]: v for v in second.get("/api/v1/models", headers=headers).json()["versions"]
    }
    assert versions["0.1.0"]["status"] == "active" and versions["0.1.0"]["serving"] is True
    assert versions["0.0.1"]["status"] == "registered" and versions["0.0.1"]["serving"] is False


def test_registering_with_mismatched_policy_or_bytes_is_refused(
    engine: Engine, trained_artifact: Path, candidate: tuple[Path, Path]
) -> None:
    from fraud_ml.models.artifact import ArtifactError

    with Session(engine) as s, pytest.raises(ValueError, match="bound to"):
        register_model(s, load_artifact(trained_artifact), DecisionPolicy.load(candidate[1]))
    _register_candidate(engine, candidate)
    tampered = load_artifact(candidate[0])
    tampered = type(tampered)(
        booster=tampered.booster, manifest={**tampered.manifest, "model_sha256": "e" * 64}
    )
    with Session(engine) as s, pytest.raises(ArtifactError, match="different artifact hash"):
        register_model(s, tampered)
