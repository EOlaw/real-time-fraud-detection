"""Raw dataset ingestion: validate, minimize, tokenize, de-duplicate, version.

Usage:
    FRAUD_TOKENIZATION_KEY=... uv run python -m fraud_ml.data.ingest \
        --raw ml/data/raw/sparkov/credit_card_transaction_train.csv \
        --raw ml/data/raw/sparkov/credit_card_transaction_test.csv \
        --out ml/data/processed [--fail-on-warning]

Inputs may be CSV or Parquet with the raw Sparkov columns; both are normalized to the same types
before validation, so they produce the same canonical output.

On success (exit 0) ``--out`` receives, in this order (the manifest is the commit marker):
  ``card_transactions.parquet``  canonical, tokenized, sorted by (event_time, event_id)
  ``data_quality_report.json``   checks, distributions, duplicate/coverage/label analysis
  ``dataset_manifest.json``      dataset version, input/output hashes, quality status
On failure (exit 1) only ``data_quality_report.failed.json`` is written; existing outputs are left
untouched. Error vs. warning conditions are defined in ``fraud_ml.data.quality``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pandera.errors
import pyarrow.parquet as pq

from fraud_ml.data.quality import (
    QualityReport,
    QualityThresholds,
    check_amounts,
    check_labels,
    check_prevalence_consistency,
    check_time,
    describe_failures,
    duplicate_analysis,
    profile_input,
    schema_failures,
)
from fraud_ml.data.schema import RAW_COLUMNS, CanonicalTransactionSchema
from fraud_ml.domains.card_payments import ASSUMED_CURRENCY, DOMAIN, derive_channel

# Version of this ingestion code (recorded in the manifest).
INGESTION_VERSION = "2"
# Version of the canonical output contract. The dataset version is derived from the input hashes
# and this contract version, so a code change that produces identical output keeps the same
# dataset version (verified for ingestion v2: byte-identical parquet to v1 on the full dataset).
CANONICAL_SCHEMA_VERSION = "1"
MIN_KEY_LENGTH = 32

DATA_FILE = "card_transactions.parquet"
MANIFEST_FILE = "dataset_manifest.json"
REPORT_FILE = "data_quality_report.json"
FAILED_REPORT_FILE = "data_quality_report.failed.json"

log = logging.getLogger(__name__)


class IngestionError(RuntimeError):
    def __init__(self, message: str, report_path: Path | None = None) -> None:
        super().__init__(message)
        self.report_path = report_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tokenize(values: pd.Series, key: bytes) -> pd.Series:
    """Keyed, deterministic pseudonymization of card numbers (HMAC-SHA256, 64-bit prefix).

    The PAN never leaves this function: only the token is written to disk. A keyed HMAC (rather
    than a plain hash) prevents brute-forcing the small PAN space back to card numbers.
    """
    return values.map(lambda v: "cus_" + hmac.new(key, v.encode(), hashlib.sha256).hexdigest()[:16])


@dataclass
class RawInput:
    name: str
    format: str
    columns: list[str] = field(default_factory=list)
    frame: pd.DataFrame | None = None
    read_error: str | None = None
    unparseable: dict[str, int] = field(default_factory=dict)

    @property
    def missing_columns(self) -> list[str]:
        return [c for c in RAW_COLUMNS if c not in self.columns]


def _as_text(series: pd.Series) -> pd.Series:
    return series.map(lambda v: None if pd.isna(v) else str(v).strip()).astype(object)


def _card_number_text(series: pd.Series) -> pd.Series:
    """Card numbers as digit strings. Parquet may store them as integers; floats are only accepted
    while exactly representable (a float64 cannot hold every 16+ digit number)."""
    if pd.api.types.is_integer_dtype(series):
        return series.map(lambda v: None if pd.isna(v) else str(int(v))).astype(object)
    if pd.api.types.is_float_dtype(series):
        return series.map(
            lambda v: (
                None if pd.isna(v) or not float(v).is_integer() or abs(v) > 2**53 else str(int(v))
            )
        ).astype(object)
    return _as_text(series)


def _normalize(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Coerce raw columns to the types the raw schema expects. Values that cannot be parsed become
    null and are counted, so they surface as schema failures instead of crashing ingestion."""
    out = pd.DataFrame(index=df.index)
    unparseable: dict[str, int] = {}

    def track(col: str, parsed: pd.Series) -> pd.Series:
        unparseable[col] = int((df[col].notna() & parsed.isna()).sum())
        return parsed

    t = df["trans_date_trans_time"]
    if not pd.api.types.is_datetime64_any_dtype(t):
        t = pd.to_datetime(t.astype(object), errors="coerce", format="ISO8601")
    if getattr(t.dt, "tz", None) is not None:
        # Timezone-aware input is converted to UTC, matching the dataset clock convention.
        t = t.dt.tz_convert("UTC").dt.tz_localize(None)
    out["trans_date_trans_time"] = track("trans_date_trans_time", t).astype("datetime64[us]")
    out["cc_num"] = track("cc_num", _card_number_text(df["cc_num"]))
    for col in ("merchant", "category", "state", "trans_num"):
        out[col] = track(col, _as_text(df[col]))
    out["amt"] = track("amt", pd.to_numeric(df["amt"], errors="coerce").astype(float))
    label = track("is_fraud", pd.to_numeric(df["is_fraud"], errors="coerce"))
    integral = bool(label.notna().all()) and bool((label == label.round()).all())
    out["is_fraud"] = label.astype("int64") if integral else label.astype(float)
    return out[list(RAW_COLUMNS)], {k: v for k, v in unparseable.items() if v}


def read_raw(path: Path) -> RawInput:
    """Read only the required raw columns from CSV or Parquet; never load other (PII) columns."""
    fmt = path.suffix.lstrip(".").lower()
    raw = RawInput(name=path.name, format=fmt)
    reader: Callable[[list[str]], pd.DataFrame]
    try:
        if fmt == "parquet":
            raw.columns = list(pq.read_schema(path).names)

            def reader(cols: list[str]) -> pd.DataFrame:
                return pd.read_parquet(path, columns=cols)
        elif fmt == "csv":
            raw.columns = list(pd.read_csv(path, nrows=0).columns)

            def reader(cols: list[str]) -> pd.DataFrame:
                return pd.read_csv(path, usecols=cols, dtype=str)
        else:
            raw.read_error = f"unsupported file type .{fmt} (expected .csv or .parquet)"
            return raw
        if raw.missing_columns:
            return raw
        raw.frame, raw.unparseable = _normalize(reader(list(RAW_COLUMNS)))
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        # Parser exception text can contain raw values; record only the exception type.
        raw.read_error = f"could not read file ({type(exc).__name__})"
    return raw


def canonicalize(df: pd.DataFrame, key: bytes, source_file: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "event_id": df["trans_num"].to_numpy(),
            # The source has no timezone. We pin the dataset clock to UTC; see dataset docs.
            "event_time": df["trans_date_trans_time"].dt.tz_localize("UTC").to_numpy(),
            "customer_id": tokenize(df["cc_num"], key).to_numpy(),
            # Every merchant name in the source carries a constant "fraud_" prefix (a generator
            # artifact, present on legitimate rows too). Strip it so it is never mistaken for a
            # label.
            "merchant_id": df["merchant"].str.removeprefix("fraud_").to_numpy(),
            "category": df["category"].to_numpy(),
            "channel": df["category"].map(derive_channel).to_numpy(),
            "amount": df["amt"].to_numpy(),
            "currency": ASSUMED_CURRENCY,
            "customer_region": df["state"].to_numpy(),
            "is_fraud": df["is_fraud"].to_numpy(),
            "source_file": source_file,
        }
    )
    out["event_time"] = pd.to_datetime(out["event_time"], utc=True).astype("datetime64[us, UTC]")
    return out


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)


def _fail(report: QualityReport, out_dir: Path, **extra: Any) -> IngestionError:
    path = out_dir / FAILED_REPORT_FILE
    _write_json_atomic(path, report.to_dict(**extra))
    log.error("ingestion failed: %d error(s); report at %s", len(report.errors), path)
    failed = report.errors or report.warnings
    return IngestionError(f"ingestion failed: {describe_failures(failed)}", report_path=path)


def _check_input(report: QualityReport, path: Path) -> pd.DataFrame | None:
    """Read and validate one input, recording checks and its profile. Returns the normalized
    frame, or None if the input cannot be used."""
    raw = read_raw(path)
    info: dict[str, Any] = {"format": raw.format}
    report.inputs[raw.name] = info
    readable = report.add(
        "input.readable",
        "error",
        raw.read_error is None,
        raw.read_error or "file readable",
        input=raw.name,
    )
    if not readable.passed:
        return None
    info["sha256"] = sha256_file(path)
    info["ignored_columns"] = sorted(set(raw.columns) - set(RAW_COLUMNS))
    has_columns = report.add(
        "input.required_columns",
        "error",
        not raw.missing_columns,
        f"{len(raw.missing_columns)} required column(s) missing",
        input=raw.name,
        missing=raw.missing_columns,
    )
    if not has_columns.passed or raw.frame is None:
        return None
    df = raw.frame
    if not report.add(
        "input.non_empty", "error", len(df) > 0, f"{len(df)} data rows", input=raw.name
    ).passed:
        return None
    failures = schema_failures(df)
    report.add(
        "schema.valid",
        "error",
        not failures and not raw.unparseable,
        f"{sum(f['failure_count'] for f in failures)} schema check failure(s); "
        f"{sum(raw.unparseable.values())} unparseable value(s)",
        input=raw.name,
        failures=failures,
        unparseable_values=raw.unparseable,
    )
    info.update(profile_input(df, report.thresholds))
    log.info("read %s (%s): %d rows", raw.name, raw.format, len(df))
    return df


def ingest(
    raw_paths: Sequence[Path],
    out_dir: Path,
    key: bytes,
    thresholds: QualityThresholds | None = None,
    fail_on_warning: bool = False,
) -> dict[str, Any]:
    if len(key) < MIN_KEY_LENGTH:
        raise IngestionError(f"Tokenization key must be at least {MIN_KEY_LENGTH} bytes")
    names = [p.name for p in raw_paths]
    if len(set(names)) != len(names):
        raise IngestionError("Input file names must be unique (they label source_file)")

    report = QualityReport(thresholds or QualityThresholds())
    meta: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ingestion_version": INGESTION_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
    }
    frames = []
    for path in raw_paths:
        df = _check_input(report, path)
        if df is not None:
            frames.append(df.assign(_input=path.name))
    if report.errors or not frames:
        raise _fail(report, out_dir, **meta)

    combined_raw = pd.concat(frames, ignore_index=True)
    deduped, dup = duplicate_analysis(combined_raw, "trans_num", list(RAW_COLUMNS))
    report.combined["duplicates"] = dup
    report.add(
        "duplicates.no_conflicting_ids",
        "error",
        dup["conflicting_ids"] == 0,
        f"{dup['conflicting_ids']} event id(s) appear with different content",
        **{k: v for k, v in dup.items() if k != "duplicate_id_rows"},
    )
    report.add(
        "duplicates.no_exact_duplicates",
        "warning",
        dup["exact_duplicates_dropped"] == 0,
        f"{dup['exact_duplicates_dropped']} exact duplicate row(s) dropped (kept first)",
    )
    check_labels(report, deduped["is_fraud"])
    check_prevalence_consistency(report)
    check_time(report, deduped["trans_date_trans_time"])
    check_amounts(report, deduped["amt"])
    if report.errors or (fail_on_warning and report.warnings):
        raise _fail(report, out_dir, **meta)

    df = pd.concat(
        [
            canonicalize(group.drop(columns="_input"), key, str(name))
            for name, group in deduped.groupby("_input", sort=False)
        ],
        ignore_index=True,
    )
    # Point-in-time order: features and replay depend on this sort.
    df = df.sort_values(["event_time", "event_id"], kind="mergesort").reset_index(drop=True)
    try:
        CanonicalTransactionSchema.validate(df, lazy=True)
        canonical_ok = True
    except pandera.errors.SchemaErrors:
        canonical_ok = False
    report.add("canonical.contract", "error", canonical_ok, "canonical output matches contract")
    if not canonical_ok:
        raise _fail(report, out_dir, **meta)

    report.combined.update(
        {
            "rows_in": len(combined_raw),
            "rows_out": len(df),
            "customers": int(df["customer_id"].nunique()),
            "fraud": int(df["is_fraud"].sum()),
            "prevalence": float(df["is_fraud"].mean()),
            "channel_rows": {str(k): int(v) for k, v in df["channel"].value_counts().items()},
        }
    )
    hashes = {name: report.inputs[name]["sha256"] for name in names}
    # Key name "ingestion" kept so dataset versions stay comparable with ingestion v1.
    version_src = json.dumps(
        {"inputs": hashes, "ingestion": CANONICAL_SCHEMA_VERSION}, sort_keys=True
    )
    dataset_version = hashlib.sha256(version_src.encode()).hexdigest()[:12]
    summary = report.to_dict()["summary"]

    out_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out_dir, prefix=".staging-"))
    try:
        df.to_parquet(staging / DATA_FILE, index=False)
        manifest = {
            "dataset": "sparkov_card_transactions",
            "domain": DOMAIN,
            "dataset_version": dataset_version,
            "ingestion_version": INGESTION_VERSION,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "created_at": meta["generated_at"],
            "inputs_sha256": hashes,
            "input_formats": {n: report.inputs[n]["format"] for n in names},
            "quality": {"status": report.status, "warnings": summary["warnings"]},
            "output": {
                "path": DATA_FILE,
                "sha256": sha256_file(staging / DATA_FILE),
                "rows": len(df),
                "dropped_duplicate_rows": dup["exact_duplicates_dropped"],
                "fraud_count": int(df["is_fraud"].sum()),
                "prevalence": float(df["is_fraud"].mean()),
                "time_range": [
                    df["event_time"].min().isoformat(),
                    df["event_time"].max().isoformat(),
                ],
                "customers": int(df["customer_id"].nunique()),
                "sort_order": ["event_time", "event_id"],
            },
            "dropped_raw_fields": "all except " + ", ".join(RAW_COLUMNS) + " (cc_num tokenized)",
        }
        _write_json_atomic(
            staging / REPORT_FILE, report.to_dict(**meta, dataset_version=dataset_version)
        )
        _write_json_atomic(staging / MANIFEST_FILE, manifest)
        # Manifest last: a manifest whose output hash matches the parquet marks a complete run.
        for name in (DATA_FILE, REPORT_FILE, MANIFEST_FILE):
            os.replace(staging / name, out_dir / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    (out_dir / FAILED_REPORT_FILE).unlink(missing_ok=True)
    log.info(
        "wrote %d rows, dataset_version=%s, quality=%s", len(df), dataset_version, report.status
    )
    return manifest


def load_key() -> bytes:
    key = os.environ.get("FRAUD_TOKENIZATION_KEY", "")
    if not key:
        raise IngestionError("FRAUD_TOKENIZATION_KEY is not set (see .env.example)")
    return key.encode()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--raw", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=Path("ml/data/processed"))
    parser.add_argument(
        "--fail-on-warning", action="store_true", help="treat quality warnings as failures"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        manifest = ingest(args.raw, args.out, load_key(), fail_on_warning=args.fail_on_warning)
    except IngestionError as exc:
        print(str(exc), file=sys.stderr)
        if exc.report_path:
            print(f"quality report: {exc.report_path}", file=sys.stderr)
        sys.exit(1)
    print(
        json.dumps(
            {
                "dataset_version": manifest["dataset_version"],
                "quality": manifest["quality"],
                **manifest["output"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
