"""Import canonical Parquet (output of ``fraud_ml.data.ingest``) into ``transactions``.

Guarantees:
* **Tenant scoped** - rows are written for one organization; conflicts are only checked within it.
* **Labeled provenance** - every row carries ``source`` (``backfill``, ``replay`` or ``simulated``;
  ``live`` is refused because a file import is never live traffic) and
  ``schema_version='canonical-<n>'``.
* **Idempotent** - a row whose ``(organization_id, external_id)`` already exists with identical
  content is counted as ``already_present`` and skipped, so retries are safe.
* **Conflicts rejected** - the same ``external_id`` with different content (including a different
  ``source``) aborts the whole import.
* **All or nothing** - staging, conflict check, insert, verification and the audit record run in
  one database transaction; any failure rolls everything back.

Fraud labels (``is_fraud``) are not imported: ``transactions`` holds events, and labels get their
own provenance-tracked table in Phase 8.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd
import pandera.errors
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fraud_api.db.models import AuditLog, Organization
from fraud_ml.data.ingest import CANONICAL_SCHEMA_VERSION, MANIFEST_FILE, sha256_file
from fraud_ml.data.schema import CanonicalTransactionSchema

IMPORTABLE_SOURCES: Final = ("backfill", "replay", "simulated")
SCHEMA_VERSION: Final = f"canonical-{CANONICAL_SCHEMA_VERSION}"
MAX_EXAMPLE_IDS: Final = 5
# Imports are bulk operations: they get their own transaction-local statement timeout instead of
# the API engine's request-path timeout (5 s).
DEFAULT_STATEMENT_TIMEOUT_MS: Final = 600_000

_STAGE_COLUMNS: Final = (
    "external_id",
    "customer_id",
    "merchant_id",
    "amount",
    "currency",
    "category",
    "channel",
    "region",
    "event_time",
    "source",
)
# Content that must match for an existing row to count as "already imported".
_COMPARED = (
    "customer_id, merchant_id, amount, currency, category, channel, region, event_time, source"
)


class TransactionImportError(RuntimeError):
    """Base class: the import was refused or rolled back; nothing was written."""


class ImportValidationError(TransactionImportError):
    pass


class ImportConflictError(TransactionImportError):
    def __init__(self, conflicting: int, examples: list[str]) -> None:
        super().__init__(
            f"{conflicting} transaction id(s) already exist in this organization with different "
            f"content (e.g. {', '.join(examples)}); nothing was imported"
        )
        self.conflicting = conflicting
        self.examples = examples


@dataclass(frozen=True)
class ImportResult:
    organization: str
    source: str
    dataset_version: str
    parquet_sha256: str
    window_start: str | None
    window_end: str | None
    rows_selected: int
    inserted: int
    already_present: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_canonical(
    data_path: Path, start: datetime | None = None, end: datetime | None = None
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load and verify canonical Parquet against its manifest, then select ``[start, end)``."""
    manifest_path = data_path.parent / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ImportValidationError(f"{MANIFEST_FILE} not found next to {data_path.name}")
    manifest = json.loads(manifest_path.read_text())
    output = manifest.get("output", {})
    if output.get("path") != data_path.name or output.get("sha256") != sha256_file(data_path):
        raise ImportValidationError(
            "Parquet file does not match its manifest (hash or name differs); re-run ingestion"
        )
    if manifest.get("canonical_schema_version", CANONICAL_SCHEMA_VERSION) != (
        CANONICAL_SCHEMA_VERSION
    ):
        raise ImportValidationError("Unsupported canonical schema version")
    df = pd.read_parquet(data_path)
    try:
        CanonicalTransactionSchema.validate(df, lazy=True)
    except pandera.errors.SchemaErrors as exc:
        raise ImportValidationError(
            f"Canonical contract violated ({len(exc.failure_cases)} failure(s))"
        ) from exc
    cents = df["amount"] * 100
    if not bool(((cents - cents.round()).abs() < 1e-6).all()):
        raise ImportValidationError("Amounts with more than 2 decimal places cannot be stored")
    if start is not None:
        df = df[df["event_time"] >= _utc(start)]
    if end is not None:
        df = df[df["event_time"] < _utc(end)]
    df = df.sort_values(["event_time", "event_id"], kind="mergesort")
    return df, manifest


def _utc(ts: datetime) -> pd.Timestamp:
    if ts.tzinfo is None:
        raise ImportValidationError("--start/--end must include a timezone offset")
    return pd.Timestamp(ts).tz_convert("UTC")


def _stage_rows(df: pd.DataFrame, source: str) -> list[tuple[str | None, ...]]:
    def text_or_none(v: Any) -> str | None:
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)

    frame = pd.DataFrame(
        {
            "external_id": df["event_id"].astype(str),
            "customer_id": df["customer_id"].astype(str),
            "merchant_id": df["merchant_id"],
            "amount": df["amount"].map(lambda a: f"{a:.2f}"),
            "currency": df["currency"],
            "category": df["category"],
            "channel": df["channel"],
            "region": df["customer_region"],
            "event_time": df["event_time"].dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
            "source": source,
        }
    )
    return [tuple(text_or_none(v) for v in row) for row in frame.itertuples(index=False)]


def import_transactions(
    session: Session,
    organization_slug: str,
    df: pd.DataFrame,
    manifest: dict[str, Any],
    source: str,
    window: tuple[datetime | None, datetime | None] = (None, None),
    actor_user_id: uuid.UUID | None = None,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> ImportResult:
    """Import rows in one transaction; commits on success, rolls back on any failure."""
    if source not in IMPORTABLE_SOURCES:
        raise ImportValidationError(
            f"source must be one of {', '.join(IMPORTABLE_SOURCES)} (file imports are never live)"
        )
    try:
        organization = session.scalar(
            select(Organization).where(Organization.slug == organization_slug)
        )
        if organization is None:
            raise ImportValidationError(f"organization {organization_slug!r} not found")
        org_id = organization.id
        conn = session.connection()
        conn.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(int(statement_timeout_ms))},
        )
        # Serialize imports per tenant so two concurrent imports cannot interleave.
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"transactions-import:{org_id}"},
        )
        conn.execute(
            text(
                """
                CREATE TEMP TABLE import_stage (
                    external_id varchar(128) PRIMARY KEY, customer_id varchar(128) NOT NULL,
                    merchant_id varchar(200), amount numeric(18,2) NOT NULL,
                    currency varchar(3) NOT NULL, category varchar(64) NOT NULL,
                    channel varchar(32) NOT NULL, region varchar(64),
                    event_time timestamptz NOT NULL, source varchar(16) NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        driver = conn.connection.driver_connection
        assert driver is not None
        with driver.cursor() as cur:
            cols = ", ".join(_STAGE_COLUMNS)
            with cur.copy(f"COPY import_stage ({cols}) FROM STDIN") as copy:
                for row in _stage_rows(df, source):
                    copy.write_row(row)

        params = {"org": org_id}
        insert_params = {**params, "schema_version": SCHEMA_VERSION, "hash_prefix": SCHEMA_VERSION}
        # Column names come from module constants; all values are bound parameters.
        conflict_sql = f"""
            FROM import_stage s JOIN transactions t
              ON t.organization_id = :org AND t.external_id = s.external_id
            WHERE ({", ".join("t." + c for c in _COMPARED.split(", "))})
                  IS DISTINCT FROM ({", ".join("s." + c for c in _COMPARED.split(", "))})
        """
        conflicts = conn.execute(text(f"SELECT count(*) {conflict_sql}"), params).scalar_one()
        if conflicts:
            examples = conn.execute(
                text(f"SELECT s.external_id {conflict_sql} ORDER BY 1 LIMIT {MAX_EXAMPLE_IDS}"),
                params,
            ).scalars()
            raise ImportConflictError(int(conflicts), [str(e) for e in examples])

        inserted = conn.execute(
            text(
                """
                INSERT INTO transactions (id, organization_id, external_id, customer_id,
                    merchant_id, amount, currency, category, channel, region, event_time, source,
                    schema_version, payload_sha256)
                SELECT gen_random_uuid(), :org, s.external_id, s.customer_id, s.merchant_id,
                    s.amount, s.currency, s.category, s.channel, s.region, s.event_time, s.source,
                    :schema_version,
                    encode(sha256(convert_to(concat_ws('|', CAST(:hash_prefix AS text),
                        s.external_id, s.customer_id, s.merchant_id, s.amount::text, s.currency,
                        s.category,
                        s.channel, s.region,
                        to_char(s.event_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                        s.source), 'UTF8')), 'hex')
                FROM import_stage s
                ORDER BY s.event_time, s.external_id
                ON CONFLICT (organization_id, external_id) DO NOTHING
                """
            ),
            insert_params,
        ).rowcount
        # Verify, inside the same transaction, that every staged row is now present unchanged
        # (guards against a concurrent non-import writer racing the conflict check).
        late = conn.execute(text(f"SELECT count(*) {conflict_sql}"), params).scalar_one()
        if late:
            raise ImportConflictError(int(late), [])

        result = ImportResult(
            organization=organization_slug,
            source=source,
            dataset_version=str(manifest.get("dataset_version")),
            parquet_sha256=str(manifest["output"]["sha256"]),
            window_start=window[0].isoformat() if window[0] else None,
            window_end=window[1].isoformat() if window[1] else None,
            rows_selected=len(df),
            inserted=int(inserted),
            already_present=len(df) - int(inserted),
        )
        session.add(
            AuditLog(
                organization_id=org_id,
                actor_user_id=actor_user_id,
                action="transactions.import",
                resource_type="transaction",
                details={k: v for k, v in result.to_dict().items() if k != "organization"},
            )
        )
        session.commit()
        return result
    except BaseException:
        session.rollback()
        raise
