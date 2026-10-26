"""PostgreSQL import of canonical Parquet: tenant scope, provenance, idempotency, atomicity."""

from __future__ import annotations

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from fraud_api import cli
from fraud_api.core.config import get_settings
from fraud_api.db import importer
from fraud_api.db.importer import (
    ImportConflictError,
    ImportResult,
    ImportValidationError,
    import_transactions,
    load_canonical,
)
from fraud_api.db.models import AuditLog, Organization, Transaction
from fraud_ml.data.ingest import DATA_FILE, ingest
from tests.fixtures import KEY, PAN_A, SENSITIVE, raw_frame, write
from tests.integration.conftest import Tenant

pytestmark = pytest.mark.integration


def canonical_file(tmp_path: Path, raw: pd.DataFrame | None = None, name: str = "c") -> Path:
    out = tmp_path / name
    ingest([write(raw if raw is not None else raw_frame(), tmp_path / f"{name}.csv")], out, KEY)
    return out / DATA_FILE


def make_org(engine: Engine, slug: str) -> Organization:
    with Session(engine, expire_on_commit=False) as s:
        org = Organization(slug=slug, name=slug)
        s.add(org)
        s.commit()
        return org


def run_import(
    engine: Engine,
    org: Organization,
    path: Path,
    source: str = "backfill",
    start: datetime | None = None,
    end: datetime | None = None,
) -> ImportResult:
    df, manifest = load_canonical(path, start, end)
    with Session(engine) as s:
        return import_transactions(s, org.slug, df, manifest, source, window=(start, end))


def count(engine: Engine, org: Organization | None = None) -> int:
    with Session(engine) as s:
        q = select(func.count()).select_from(Transaction)
        if org is not None:
            q = q.where(Transaction.organization_id == org.id)
        return int(s.scalar(q) or 0)


# ---------------------------------------------------------------- happy path


def test_import_writes_tenant_scoped_labeled_rows(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)
    result = run_import(engine, org, path)
    assert (result.rows_selected, result.inserted, result.already_present) == (8, 8, 0)
    expected = pd.read_parquet(path)
    with Session(engine) as s:
        rows = s.scalars(
            select(Transaction).order_by(Transaction.event_time, Transaction.external_id)
        ).all()
        audit = s.scalars(select(AuditLog).where(AuditLog.action == "transactions.import")).one()
    assert [r.external_id for r in rows] == expected["event_id"].tolist()
    assert {r.organization_id for r in rows} == {org.id}
    assert {r.source for r in rows} == {"backfill"}
    assert {r.schema_version for r in rows} == {"canonical-1"}
    assert rows[4].amount == Decimal("1250.75")
    assert rows[0].event_time == expected["event_time"].iloc[0].to_pydatetime()
    assert all(len(r.payload_sha256) == 64 for r in rows)
    assert not hasattr(Transaction, "is_fraud")  # labels are not imported
    assert audit.details["inserted"] == 8
    assert audit.details["dataset_version"] == result.dataset_version


def test_reimport_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)
    run_import(engine, org, path)
    again = run_import(engine, org, path)
    assert (again.inserted, again.already_present) == (0, 8)
    assert count(engine) == 8
    with Session(engine) as s:
        assert s.scalar(select(func.count()).where(AuditLog.action == "transactions.import")) == 2


def test_overlapping_windows_only_insert_new_rows(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)
    t0 = datetime(2019, 1, 1, tzinfo=UTC)
    first = run_import(engine, org, path, start=t0, end=t0 + timedelta(minutes=4))
    # Half-open window: minutes 0,1,1(tie),3 -> 4 rows; the event at exactly +4 min is excluded.
    assert first.inserted == 4
    second = run_import(engine, org, path)
    assert (second.inserted, second.already_present) == (4, 4)


def test_concurrent_imports_of_same_file_insert_once(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: run_import(engine, org, path), range(4)))
    assert sorted(r.inserted for r in results) == [0, 0, 0, 8]
    assert count(engine) == 8


# ---------------------------------------------------------------- conflicts and atomicity


def test_conflicting_row_aborts_entire_import(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    run_import(engine, org, canonical_file(tmp_path, name="v1"))

    changed = raw_frame(n=8)
    changed.loc[3, "amt"] = 4.98  # same event id, different amount
    extra = raw_frame(n=8, start="2019-01-02 00:00:00").assign(
        trans_num=[f"{i + 500:032x}" for i in range(8)]
    )
    v2 = canonical_file(tmp_path, pd.concat([changed, extra]), name="v2")
    with pytest.raises(ImportConflictError) as exc:
        run_import(engine, org, v2)
    assert exc.value.conflicting == 1
    assert exc.value.examples == [f"{3:032x}"]
    assert count(engine) == 8  # none of the 8 new events were written
    with Session(engine) as s:
        assert s.scalar(
            select(Transaction.amount).where(Transaction.external_id == f"{3:032x}")
        ) == Decimal("4.99")


def test_different_source_label_is_a_conflict(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)
    run_import(engine, org, path, source="backfill")
    with pytest.raises(ImportConflictError):
        run_import(engine, org, path, source="replay")


def test_live_source_is_refused(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    with pytest.raises(ImportValidationError, match="never live"):
        run_import(engine, org, canonical_file(tmp_path), source="live")


def test_failure_after_insert_rolls_back_everything(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = make_org(engine, "acme")
    path = canonical_file(tmp_path)

    def boom(**_: Any) -> None:
        raise RuntimeError("simulated failure after insert")

    monkeypatch.setattr(importer, "AuditLog", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        run_import(engine, org, path)
    assert count(engine) == 0
    monkeypatch.undo()
    assert run_import(engine, org, path).inserted == 8  # temp table and lock were released


def test_tampered_parquet_is_refused(tmp_path: Path) -> None:
    path = canonical_file(tmp_path)
    df = pd.read_parquet(path)
    df.loc[0, "amount"] = 1.0
    df.to_parquet(path, index=False)
    with pytest.raises(ImportValidationError, match="does not match its manifest"):
        load_canonical(path)


def test_window_requires_timezone(tmp_path: Path) -> None:
    with pytest.raises(ImportValidationError, match="timezone"):
        load_canonical(canonical_file(tmp_path), start=datetime(2019, 1, 1))


# ---------------------------------------------------------------- tenant isolation


def test_tenants_are_isolated(engine: Engine, tmp_path: Path) -> None:
    acme, globex = make_org(engine, "acme"), make_org(engine, "globex")
    run_import(engine, acme, canonical_file(tmp_path, name="v1"))
    changed = raw_frame()
    changed.loc[3, "amt"] = 4.98
    v2 = canonical_file(tmp_path, changed, name="v2")
    with pytest.raises(ImportConflictError):
        run_import(engine, acme, v2)
    # The same ids in another organization are an independent namespace.
    assert run_import(engine, globex, v2).inserted == 8
    assert (count(engine, acme), count(engine, globex)) == (8, 8)


# ---------------------------------------------------------------- interaction with scoring


def test_imported_history_feeds_scoring_and_ids_are_not_rescored(
    engine: Engine, client: TestClient, tenant: Tenant, tmp_path: Path
) -> None:
    path = canonical_file(tmp_path)
    with Session(engine, expire_on_commit=False) as s:
        org = s.get(Organization, tenant.org_id)
        assert org is not None
    run_import(engine, org, path)
    customer = pd.read_parquet(path).iloc[0]["customer_id"]  # token of PAN_A
    body = {
        "transaction_id": "after_import",
        "customer_id": customer,
        "amount": "42.00",
        "currency": "USD",
        "category": "home",
        "event_time": "2019-01-01T01:00:00+00:00",
        "source": "replay",
    }
    scored = client.post("/api/v1/fraud/score", json=body, headers=tenant.headers())
    assert scored.status_code == 200, scored.text
    detail = client.get(
        f"/api/v1/fraud/predictions/{scored.json()['assessment_id']}",
        headers=tenant.headers(),
    ).json()
    assert detail["features"]["cust_txn_count_1h"] == 4  # PAN_A rows at minutes 0, 2, 4, 6

    existing = {**body, "transaction_id": pd.read_parquet(path).iloc[0]["event_id"]}
    r = client.post("/api/v1/fraud/score", json=existing, headers=tenant.headers())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "transaction_exists_unscored"


def test_database_rows_contain_no_sensitive_raw_values(engine: Engine, tmp_path: Path) -> None:
    org = make_org(engine, "acme")
    run_import(engine, org, canonical_file(tmp_path))
    with engine.connect() as conn:
        dump = " ".join(
            str(row)
            for table in ("transactions", "audit_logs")
            for row in conn.execute(text(f"SELECT * FROM {table}"))  # noqa: S608 - fixed names
        )
    for value in SENSITIVE:
        assert value not in dump
    assert PAN_A not in dump


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAUD_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAUD_JWT_SECRET", secrets.token_urlsafe(48))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_import_retry_and_conflict_exit_codes(
    engine: Engine, tmp_path: Path, cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    make_org(engine, "acme")
    path = canonical_file(tmp_path, name="v1")
    argv = ["import-transactions", "--org", "acme", "--data", str(path), "--source", "backfill"]
    cli.main(argv)
    assert json.loads(capsys.readouterr().out)["inserted"] == 8
    cli.main(argv)
    assert json.loads(capsys.readouterr().out)["already_present"] == 8

    # Phase 1 command keeps working and goes through the same idempotent importer.
    cli.main(
        [
            "load-history",
            "--org",
            "acme",
            "--data",
            str(path),
            "--start",
            "2019-01-01T00:00:00+00:00",
            "--end",
            "2019-01-02T00:00:00+00:00",
        ]
    )
    assert json.loads(capsys.readouterr().out)["already_present"] == 8

    changed = raw_frame()
    changed.loc[0, "amt"] = 11.0
    v2 = canonical_file(tmp_path, changed, name="v2")
    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["import-transactions", "--org", "acme", "--data", str(v2), "--source", "backfill"]
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "import failed" in err and "nothing was imported" in err
    assert count(engine) == 8


def test_unknown_organization_is_refused(engine: Engine, tmp_path: Path) -> None:
    df, manifest = load_canonical(canonical_file(tmp_path))
    with Session(engine) as s, pytest.raises(ImportValidationError, match="not found"):
        import_transactions(s, "no-such-org", df, manifest, "backfill")
    assert count(engine) == 0


def test_statement_timeout_rolls_back_and_cli_reports_cleanly(
    engine: Engine, tmp_path: Path, cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    org = make_org(engine, "acme")
    df, manifest = load_canonical(canonical_file(tmp_path))
    with engine.connect() as conn:  # hold the tenant import lock so the import must wait
        conn.execute(
            text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": f"transactions-import:{org.id}"}
        )
        with Session(engine) as s, pytest.raises(OperationalError, match="statement timeout"):
            import_transactions(s, "acme", df, manifest, "backfill", statement_timeout_ms=200)
        with pytest.raises(SystemExit) as exc:
            cli.main(
                [
                    "import-transactions",
                    "--org",
                    "acme",
                    "--data",
                    str(tmp_path / "c" / DATA_FILE),
                    "--source",
                    "backfill",
                    "--statement-timeout-s",
                    "1",
                ]
            )
        conn.execute(text("SELECT pg_advisory_unlock_all()"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "rolled back" in err and "Traceback" not in err
    assert count(engine) == 0
