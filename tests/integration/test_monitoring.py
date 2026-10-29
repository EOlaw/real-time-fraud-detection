"""Phase 9: reference profiles, monitoring runs, promotion gates (PostgreSQL)."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from fraud_api.core.security import Role
from fraud_api.db.importer import import_transactions
from fraud_api.db.models import (
    AuditLog,
    FraudLabel,
    ModelVersion,
    MonitoringMetric,
    MonitoringReference,
    MonitoringRun,
    Transaction,
    User,
)
from fraud_api.insights.analytics import Window
from fraud_api.monitoring import gates as gates_mod
from fraud_api.monitoring.drift import CALC_VERSION
from fraud_api.monitoring.gates import check_all, feature_contract, monitoring_failures
from fraud_api.monitoring.job import acknowledge, run_monitoring
from fraud_api.monitoring.reference import MonitoringError, build_profile, store_reference
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.data.ingest import MANIFEST_FILE, sha256_file
from fraud_ml.models.artifact import load_artifact
from tests.fixtures import synthetic_events
from tests.integration.conftest import Tenant, create_tenant

pytestmark = pytest.mark.integration

DATASET = {"dataset_version": "synthetic-fixture"}
EVENTS = synthetic_events(n=6_000, n_customers=40, seed=11)  # the test model's training data


def window_now() -> Window:
    now = datetime.now(UTC)
    return Window(
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        "processed",
        ("live", "replay", "simulated", "backfill"),
    )


def store_ref(engine: Engine, artifact: Path, policy_path: Path) -> MonitoringReference:
    model, policy = load_artifact(artifact), DecisionPolicy.load(policy_path)
    profile, start, end = build_profile(model, policy, EVENTS)
    with Session(engine, expire_on_commit=False) as s:
        ref, _ = store_reference(s, model, policy, DATASET, profile, (start, end))
        s.expunge(ref)
        return ref


def items(df: pd.DataFrame, amount_factor: float = 1.0) -> list[dict[str, Any]]:
    return [
        {
            "transaction_id": r.event_id,
            "customer_id": r.customer_id,
            "merchant_id": r.merchant_id,
            "amount": f"{r.amount * amount_factor:.2f}",
            "currency": "USD",
            "category": r.category,
            "event_time": r.event_time.isoformat(),
            "region": r.customer_region,
            "source": "replay",
        }
        for r in df.itertuples()
    ]


@dataclass
class Seeded:
    tenant: Tenant
    window_events: pd.DataFrame
    ref: MonitoringReference


def seed(
    client: TestClient,
    engine: Engine,
    tenant: Tenant,
    slug: str,
    ref: MonitoringReference,
    amount_factor: float = 1.0,
) -> Seeded:
    """History before the reference window as unscored backfill (so online = offline features),
    then score the reference window's events through the batch API."""
    start = pd.Timestamp(ref.window_start)
    history = EVENTS[EVENTS["event_time"] < start]
    with Session(engine, expire_on_commit=False) as s:
        import_transactions(
            s,
            slug,
            history,
            {**DATASET, "output": {"path": "fixture.parquet", "sha256": "0" * 64}},
            "backfill",
        )
    in_window = EVENTS[
        (EVENTS["event_time"] >= start) & (EVENTS["event_time"] <= pd.Timestamp(ref.window_end))
    ]
    in_window = in_window.sort_values(["event_time", "event_id"])
    body = items(in_window, amount_factor)
    for k in range(0, len(body), 500):
        r = client.post(
            "/api/v1/fraud/batch",
            json={"items": body[k : k + 500]},
            headers=tenant.headers(Role.SERVICE),
        )
        assert r.status_code == 200 and r.json()["complete"], r.text
    return Seeded(tenant, in_window, ref)


def label_dataset(engine: Engine, org_id: uuid.UUID, df: pd.DataFrame, labeled_at: datetime) -> int:
    with Session(engine, expire_on_commit=False) as s:
        ids = dict(
            s.execute(
                select(Transaction.external_id, Transaction.id).where(
                    Transaction.organization_id == org_id,
                    Transaction.external_id.in_(list(df["event_id"])),
                )
            ).all()
        )
        for r in df.itertuples():
            s.add(
                FraudLabel(
                    organization_id=org_id,
                    transaction_id=ids[r.event_id],
                    label="fraud" if r.is_fraud else "legitimate",
                    source="dataset",
                    confidence="confirmed",
                    labeled_at=labeled_at,
                    provenance={},
                )
            )
        s.commit()
    return len(df)


def snapshot(engine: Engine) -> str:
    """Hash of everything monitoring and gates must never change."""
    with engine.connect() as c:
        parts = [
            c.execute(
                text(
                    "SELECT string_agg(id::text||risk_score::text||policy_decision||decision"
                    "||status"
                    "||rules_status||coalesce(model_version_id::text,''), ',' ORDER BY id) "
                    "FROM risk_assessments"
                )
            ).scalar(),
            c.execute(
                text("SELECT string_agg(id::text||status||version, ',' ORDER BY id) FROM cases")
            ).scalar(),
            c.execute(
                text(
                    "SELECT string_agg(id::text||label||source, ',' ORDER BY id) FROM fraud_labels"
                )
            ).scalar(),
            c.execute(
                text(
                    "SELECT string_agg(name||version||status||artifact_sha256, ','"
                    " ORDER BY name, version) FROM model_versions"
                )
            ).scalar(),
            c.execute(text("SELECT count(*)::text FROM transactions")).scalar(),
        ]
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


@pytest.fixture
def ref(
    engine: Engine, client: TestClient, trained_artifact: Path, policy_path: Path
) -> MonitoringReference:
    return store_ref(engine, trained_artifact, policy_path)


# ---------------------------------------------------------------- references


def test_reference_is_versioned_immutable_and_checked(
    engine: Engine, ref: MonitoringReference, trained_artifact: Path, policy_path: Path
) -> None:
    assert ref.calc_version == CALC_VERSION and ref.dataset_version == "synthetic-fixture"
    assert ref.sample_size > 500 and set(ref.profile) >= {
        "features",
        "categorical",
        "score",
        "decisions",
    }
    again = store_ref(engine, trained_artifact, policy_path)
    assert again.id == ref.id  # identical profile: no-op
    model, policy = load_artifact(trained_artifact), DecisionPolicy.load(policy_path)
    profile, start, end = build_profile(model, policy, EVENTS)
    with Session(engine, expire_on_commit=False) as s:
        with pytest.raises(MonitoringError, match="dataset version"):
            store_reference(s, model, policy, {"dataset_version": "other"}, profile, (start, end))
        with pytest.raises(MonitoringError, match="immutable"):
            store_reference(s, model, policy, DATASET, {**profile, "n": 1}, (start, end))
    for sql in (
        "UPDATE monitoring_references SET sample_size = 1",
        "DELETE FROM monitoring_references",
    ):
        with engine.begin() as c, pytest.raises(DBAPIError, match="append-only"):
            c.execute(text(sql))


# ---------------------------------------------------------------- runs


def test_empty_window_is_insufficient_not_ok(engine: Engine, ref: MonitoringReference) -> None:
    with Session(engine, expire_on_commit=False) as s:
        run = run_monitoring(
            s,
            organization_id=None,
            model_version_id=None,
            window=window_now(),
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        statuses = {
            m.status
            for m in s.scalars(
                select(MonitoringMetric).where(
                    MonitoringMetric.run_id == run.id,
                    MonitoringMetric.labeled.is_(False),
                    MonitoringMetric.metric.like("%psi%"),
                )
            )
        }
    assert run.status == "insufficient_data" and run.recommendation == "none"
    assert statuses == {"insufficient_data"}
    assert run.summary["assessments"] == 0


def test_runs_are_idempotent_tenant_scoped_and_detect_known_drift(
    client: TestClient, engine: Engine, tenant: Tenant, ref: MonitoringReference
) -> None:
    before = snapshot(engine)  # empty-ish; taken again after seeding below
    a = seed(client, engine, tenant, "acme", ref)
    other = create_tenant(engine, client, "globex")
    seed(client, engine, other, "globex", ref, amount_factor=6.0)  # strongly shifted amounts
    before = snapshot(engine)
    w = window_now()
    with Session(engine, expire_on_commit=False) as s:
        run_a = run_monitoring(
            s,
            organization_id=tenant.org_id,
            model_version_id=None,
            window=w,
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        first = {
            (m.metric, m.segment): (m.value, m.ci_low, m.status)
            for m in s.scalars(select(MonitoringMetric).where(MonitoringMetric.run_id == run_a.id))
        }
        run_a2 = run_monitoring(
            s,
            organization_id=tenant.org_id,
            model_version_id=None,
            window=w,
            labels_as_of=run_a.labels_as_of,
            maturity_days=30,
        )
        second = {
            (m.metric, m.segment): (m.value, m.ci_low, m.status)
            for m in s.scalars(select(MonitoringMetric).where(MonitoringMetric.run_id == run_a2.id))
        }
        run_b = run_monitoring(
            s,
            organization_id=other.org_id,
            model_version_id=None,
            window=w,
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        run_g = run_monitoring(
            s,
            organization_id=None,
            model_version_id=None,
            window=w,
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        b_amount = s.scalar(
            select(MonitoringMetric.value).where(
                MonitoringMetric.run_id == run_b.id,
                MonitoringMetric.metric == "feature_psi:log_amount",
            )
        )
        runs = s.scalar(select(func.count()).select_from(MonitoringRun))
    # idempotent: same run row, identical metrics (bootstrap seeded by the run identity)
    assert run_a2.id == run_a.id and first == second and runs == 3
    n = len(a.window_events)
    assert run_a.summary["assessments"] == n and run_b.summary["assessments"] == n
    assert run_g.summary["assessments"] == 2 * n
    a_amount = first[("feature_psi:log_amount", "all")][0]
    assert a_amount is not None and a_amount < 0.1  # same distribution as the reference
    assert b_amount is not None and b_amount > 0.25 and run_b.status == "alert"
    assert run_b.recommendation == "retraining_review"
    assert "feature_psi:log_amount[all]" in run_b.summary["alerts"]
    assert first[("score_psi", "all")][1] is not None  # bootstrap interval present
    assert ("score_psi", "source:replay") in first
    # API: tenants see only their own organization's runs; global and foreign ids are 404
    h = tenant.headers(Role.VIEWER)
    listed = client.get("/api/v1/monitoring/runs", headers=h).json()
    assert [r["id"] for r in listed] == [str(run_a.id)]
    assert client.get(f"/api/v1/monitoring/runs/{run_b.id}", headers=h).status_code == 404
    assert client.get(f"/api/v1/monitoring/runs/{run_g.id}", headers=h).status_code == 404
    detail = client.get(f"/api/v1/monitoring/runs/{run_a.id}", headers=h).json()
    assert {m["labeled"] for m in detail["metrics"]} == {True, False}
    assert (
        client.get("/api/v1/monitoring/runs", headers=tenant.headers(Role.SERVICE)).status_code
        == 403
    )
    # monitoring changed nothing it reads
    assert snapshot(engine) == before


def test_other_model_versions_and_reference_mismatch(
    client: TestClient, engine: Engine, tenant: Tenant, ref: MonitoringReference
) -> None:
    a = seed(client, engine, tenant, "acme", ref)
    with Session(engine, expire_on_commit=False) as s:
        other = ModelVersion(
            name="test-model",
            version="9.9.9",
            artifact_sha256="f" * 64,
            manifest={},
            status="registered",
        )
        s.add(other)
        s.flush()
        s.execute(
            text(
                "UPDATE risk_assessments SET model_version_id = :o WHERE id IN "
                "(SELECT id FROM risk_assessments ORDER BY id LIMIT 100)"
            ),
            {"o": other.id},
        )
        s.commit()
        run = run_monitoring(
            s,
            organization_id=None,
            model_version_id=None,
            window=window_now(),
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        assert run.summary["excluded_other_model_versions"] == 100
        assert run.summary["assessments"] == len(a.window_events) - 100
        with pytest.raises(MonitoringError, match="no reference profile"):
            run_monitoring(
                s,
                organization_id=None,
                model_version_id=other.id,
                window=window_now(),
                labels_as_of=datetime.now(UTC),
                maturity_days=30,
            )
        # a reference whose artifact hash no longer matches the registry is refused
        s.execute(
            text(
                "ALTER TABLE monitoring_references DISABLE TRIGGER "
                "monitoring_references_no_update_delete"
            )
        )
        s.execute(text("UPDATE monitoring_references SET artifact_sha256 = :h"), {"h": "0" * 64})
        s.execute(
            text(
                "ALTER TABLE monitoring_references ENABLE TRIGGER "
                "monitoring_references_no_update_delete"
            )
        )
        s.commit()
        with pytest.raises(MonitoringError, match="reference_version_mismatch"):
            run_monitoring(
                s,
                organization_id=None,
                model_version_id=None,
                window=window_now(),
                labels_as_of=datetime.now(UTC),
                maturity_days=30,
            )


def test_labeled_metrics_respect_maturity_cutoff_and_minimums(
    client: TestClient, engine: Engine, tenant: Tenant, ref: MonitoringReference
) -> None:
    a = seed(client, engine, tenant, "acme", ref)
    now = datetime.now(UTC)
    cohort = Window(
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 4, 1, tzinfo=UTC),
        "event",
        ("live", "replay", "simulated"),
    )

    def labeled(as_of: datetime, maturity: int) -> dict[str, MonitoringMetric]:
        with Session(engine, expire_on_commit=False) as s:
            run = run_monitoring(
                s,
                organization_id=tenant.org_id,
                model_version_id=None,
                window=window_now(),
                labels_as_of=as_of,
                maturity_days=maturity,
                label_cohort=cohort,
            )
            return {
                m.metric: m
                for m in s.scalars(
                    select(MonitoringMetric).where(
                        MonitoringMetric.run_id == run.id, MonitoringMetric.labeled.is_(True)
                    )
                )
            }

    no_labels = labeled(now, 30)
    assert no_labels["precision_final"].status == "insufficient_data"
    assert no_labels["precision_final"].value is None
    assert no_labels["label_coverage"].value == 0.0  # unlabeled are excluded, not legitimate
    label_dataset(engine, tenant.org_id, a.window_events, labeled_at=now - timedelta(days=1))
    m = labeled(now, 30)
    positives = int(a.window_events["is_fraud"].sum())
    d = m["precision_final"].details
    assert d["usable"] == len(a.window_events) and d["usable_fraud"] == positives
    expected_status = (
        "ok" if positives >= 20 and len(a.window_events) >= 200 else "insufficient_data"
    )
    assert m["precision_final"].status == expected_status
    assert d["final_tp"] + d["final_fn"] == positives
    assert "not unbiased ground truth" in d["caveat"]
    # labels known only after the cutoff are not used
    early = labeled(now - timedelta(days=2), 30)
    assert early["precision_final"].details["usable"] == 0
    # a maturity window reaching past the cohort makes everything immature
    immature = labeled(now, 365 * 20)
    assert immature["label_coverage"].details["immature"] == len(a.window_events)


def test_acknowledgement_and_status_change(
    client: TestClient, engine: Engine, tenant: Tenant, ref: MonitoringReference
) -> None:
    other = create_tenant(engine, client, "globex")
    seed(client, engine, other, "globex", ref, amount_factor=6.0)
    with Session(engine, expire_on_commit=False) as s:
        admin = s.scalar(
            select(User.id).where(User.organization_id == other.org_id, User.role == "admin")
        )
        run = run_monitoring(
            s,
            organization_id=other.org_id,
            model_version_id=None,
            window=window_now(),
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        assert run.status == "alert"
        serving = s.scalar(select(ModelVersion.id).where(ModelVersion.status == "active"))
        assert not monitoring_failures(s, serving, datetime.now(UTC)).passed
        acknowledge(s, run.id, admin, "Known synthetic shift in this tenant")
        assert monitoring_failures(s, serving, datetime.now(UTC)).passed
        # rerun with the same result keeps the acknowledgement
        rerun = run_monitoring(
            s,
            organization_id=other.org_id,
            model_version_id=None,
            window=Window(
                run.window_start,
                run.window_end,
                "processed",
                ("live", "replay", "simulated", "backfill"),
            ),
            labels_as_of=datetime.now(UTC),
            maturity_days=30,
        )
        assert rerun.id == run.id and rerun.acknowledged_at is not None
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "monitoring.acknowledge")
            )
            == 1
        )
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            s.execute(text("DELETE FROM monitoring_runs"))


# ---------------------------------------------------------------- promotion gates


def _canonical(tmp: Path) -> Path:
    ev = EVENTS.assign(event_time=lambda d: d["event_time"].astype("datetime64[us, UTC]"))
    path = tmp / "card_transactions.parquet"
    ev.to_parquet(path, index=False)
    (tmp / MANIFEST_FILE).write_text(
        json.dumps(
            {
                "dataset_version": "synthetic-fixture",
                "canonical_schema_version": "1",
                "output": {"path": path.name, "sha256": sha256_file(path)},
            }
        )
    )
    return path


def _bound_policy(tmp: Path, artifact: Path) -> Path:
    m = load_artifact(artifact)
    review = float(m.manifest["decision_threshold"]["value"])
    path = tmp / "candidate_policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": "gate-test-policy",
                "domain": "card_payments",
                "mode": "sandbox",
                "model": {"name": m.name, "version": m.version},
                "thresholds": {
                    "challenge_at": review / 2,
                    "review_at": review,
                    "decline_at": max(review, 0.95),
                },
                "on_scoring_failure": "review",
            }
        )
    )
    return path


def test_gates_fail_closed_with_reasons(
    client: TestClient,
    engine: Engine,
    tenant: Tenant,
    ref: MonitoringReference,
    trained_artifact: Path,
    policy_path: Path,
    tmp_path: Path,
) -> None:
    data = _canonical(tmp_path)
    with Session(engine, expire_on_commit=False) as s:
        # the conftest policy's review_at is not the artifact's validation threshold
        g = check_all(
            s, trained_artifact, policy_path, data, trained_artifact, window_now(), parity_sample=50
        )
    names = {x.name: x for x in g}
    assert not names["threshold"].passed
    policy = _bound_policy(tmp_path, trained_artifact)
    with Session(engine, expire_on_commit=False) as s:
        g = {
            x.name: x
            for x in check_all(
                s, trained_artifact, policy, data, trained_artifact, window_now(), parity_sample=50
            )
        }
    assert g["artifact"].passed and g["policy"].passed and g["threshold"].passed
    assert g["feature_contract"].passed and g["reproducibility"].passed and g["reference"].passed
    # no stored scores or labels: evidence gates fail closed
    assert not g["min_samples"].passed and "0 scored" in g["min_samples"].detail
    assert not g["drift"].passed and not g["performance"].passed
    # feature contract and reproducibility failures
    fake = SimpleNamespace(manifest={"feature_columns": ["x"], "feature_spec_version": "card-v1"})
    assert not feature_contract(fake, None).passed  # type: ignore[arg-type]
    bad = tmp_path / "bad"
    bad.mkdir()
    shutil.copy(data, bad / data.name)
    (bad / MANIFEST_FILE).write_text(json.dumps({"dataset_version": "other"}))
    with Session(engine, expire_on_commit=False) as s:
        r = gates_mod.reproducibility(s, load_artifact(trained_artifact), bad / data.name)
    assert not r.passed and "dataset version" in r.detail
    # an exception inside a gate is a failed gate, not a crash or a pass
    assert not gates_mod._guard("x", lambda: 1 / 0).passed


def test_gates_pass_promote_and_rollback_are_audited(
    client: TestClient,
    engine: Engine,
    tenant: Tenant,
    ref: MonitoringReference,
    trained_artifact: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_url: str,
) -> None:
    a = seed(client, engine, tenant, "acme", ref)
    label_dataset(engine, tenant.org_id, a.window_events, datetime.now(UTC) - timedelta(days=1))
    data = _canonical(tmp_path)
    policy = _bound_policy(tmp_path, trained_artifact)
    before = snapshot(engine)
    # The synthetic test model is not calibrated well enough for production limits; loosen them
    # here to exercise the pass path (the real limits are tested to fail above).
    monkeypatch.setattr(gates_mod, "MAX_ECE", 1.0)
    monkeypatch.setattr(gates_mod, "MIN_LABELS", 50)
    monkeypatch.setattr(gates_mod, "MIN_POSITIVES", 1)
    monkeypatch.setattr(gates_mod, "MIN_SCORED", 500)
    with Session(engine, expire_on_commit=False) as s:
        g = check_all(
            s, trained_artifact, policy, data, trained_artifact, window_now(), parity_sample=50
        )
    failed = [x for x in g if not x.passed]
    assert not failed, failed
    assert snapshot(engine) == before  # gates only read

    # Promote and roll back through the CLI in a sandbox serving layout (audited).
    root = tmp_path / "serving"
    (root / "artifacts").mkdir(parents=True)
    cand = root / "artifacts" / "candidate"
    shutil.copytree(trained_artifact, cand)
    old = root / "artifacts" / "old"
    shutil.copytree(trained_artifact, old)
    (root / "artifacts" / "current").symlink_to("old")
    active = root / "active.yaml"
    shutil.copy(policy, active)
    monkeypatch.setenv("FRAUD_DATABASE_URL", db_url)
    from fraud_api.core.config import get_settings
    from fraud_api.monitoring import cli

    get_settings.cache_clear()
    now = datetime.now(UTC)
    common = [
        "--policy",
        str(policy),
        "--data",
        str(data),
        "--incumbent",
        str(old),
        "--start",
        (now - timedelta(hours=1)).isoformat(),
        "--end",
        (now + timedelta(hours=1)).isoformat(),
        "--parity-sample",
        "50",
    ]
    with pytest.raises(SystemExit) as exc:
        cli.main(["promote", "--artifact", str(cand), *common, "--operator", "tester"])
    assert exc.value.code == 0 and (root / "artifacts" / "current").resolve().name == "old"
    cli.main(
        [
            "promote",
            "--artifact",
            str(cand),
            *common,
            "--operator",
            "tester",
            "--apply",
            "--current-link",
            str(root / "artifacts" / "current"),
            "--active-policy",
            str(active),
            "--archive-dir",
            str(root / "archive"),
        ]
    )
    assert (root / "artifacts" / "current").resolve().name == "candidate"
    cli.main(
        [
            "rollback",
            "--artifact",
            str(old),
            "--policy",
            str(policy),
            "--data",
            str(data),
            "--operator",
            "tester",
            "--current-link",
            str(root / "artifacts" / "current"),
            "--active-policy",
            str(active),
        ]
    )
    assert (root / "artifacts" / "current").resolve().name == "old"
    get_settings.cache_clear()
    with Session(engine, expire_on_commit=False) as s:
        actions = s.scalars(
            select(AuditLog.action).where(AuditLog.action.like("model.%")).order_by(AuditLog.id)
        ).all()
        details = s.scalars(
            select(AuditLog.details).where(AuditLog.action == "model.promote")
        ).one()
    assert actions == ["model.promote", "model.rollback"]
    assert details["operator"] == "tester" and all(details["gates"].values())
    assert snapshot(engine) == before  # promotion repoints files only; no data changes


def test_legacy_promote_apply_is_refused(
    trained_artifact: Path, policy_path: Path, tmp_path: Path
) -> None:
    from fraud_api import cli as legacy

    with pytest.raises(SystemExit) as exc:
        legacy.main(
            [
                "promote-model",
                "--artifact",
                str(trained_artifact),
                "--policy",
                str(_bound_policy(tmp_path, trained_artifact)),
                "--data",
                str(_canonical(tmp_path)),
                "--parity-sample",
                "20",
                "--apply",
            ]
        )
    assert exc.value.code == 2
