"""Raw dataset ingestion: validate, minimize, tokenize, version.

Usage:
    FRAUD_TOKENIZATION_KEY=... uv run python -m fraud_ml.data.ingest \
        --raw ml/data/raw/sparkov/credit_card_transaction_train.csv \
        --raw ml/data/raw/sparkov/credit_card_transaction_test.csv \
        --out ml/data/processed

Outputs ``card_transactions.parquet`` (canonical schema), ``dataset_manifest.json`` (version,
hashes, row counts) and ``data_quality_report.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pandera.errors

from fraud_ml.data.schema import RAW_COLUMNS, CanonicalTransactionSchema, RawTransactionSchema
from fraud_ml.domains.card_payments import ASSUMED_CURRENCY, DOMAIN, derive_channel

INGESTION_VERSION = "1"
MIN_KEY_LENGTH = 32


class IngestionError(RuntimeError):
    pass


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


def read_raw(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        df = pd.read_parquet(path, columns=list(RAW_COLUMNS))
    elif path.suffix == ".csv":
        df = pd.read_csv(
            path,
            usecols=list(RAW_COLUMNS),
            dtype={"cc_num": str, "trans_num": str, "merchant": str, "category": str, "state": str},
        )
    else:
        raise IngestionError(f"Unsupported file type: {path.suffix} (expected .csv or .parquet)")
    df["trans_date_trans_time"] = pd.to_datetime(df["trans_date_trans_time"], format="ISO8601")
    df["amt"] = df["amt"].astype(float)
    df["is_fraud"] = df["is_fraud"].astype(int)
    for col in ("cc_num", "trans_num", "merchant", "category", "state"):
        df[col] = df[col].astype(object)
    return df


def quality_report(df: pd.DataFrame) -> dict[str, Any]:
    """Data quality summary computed on the raw (pre-minimization) frame."""
    amt = df["amt"]
    q1, q3 = amt.quantile([0.25, 0.75])
    iqr = q3 - q1
    by_month = df.groupby(df["trans_date_trans_time"].dt.to_period("M"))["is_fraud"].agg(
        ["count", "sum"]
    )
    return {
        "rows": len(df),
        "missing_values": {c: int(n) for c, n in df.isna().sum().items()},
        "duplicate_event_ids": int(df["trans_num"].duplicated().sum()),
        "duplicate_full_rows": int(df.duplicated().sum()),
        "time_range": [
            df["trans_date_trans_time"].min().isoformat(),
            df["trans_date_trans_time"].max().isoformat(),
        ],
        "distinct_cards": int(df["cc_num"].nunique()),
        "label_prevalence": float(df["is_fraud"].mean()),
        "fraud_count": int(df["is_fraud"].sum()),
        "amount": {
            "min": float(amt.min()),
            "max": float(amt.max()),
            "quantiles": {str(q): float(v) for q, v in amt.quantile([0.01, 0.5, 0.99]).items()},
            "iqr_outliers_3x": int((amt > q3 + 3 * iqr).sum()),
            "note": "Outliers are reported, not removed: large amounts are legitimate signal.",
        },
        "category_counts": {k: int(v) for k, v in df["category"].value_counts().items()},
        "monthly": {
            str(p): {"rows": int(r["count"]), "fraud": int(r["sum"])}
            for p, r in by_month.iterrows()
        },
    }


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


def ingest(raw_paths: Sequence[Path], out_dir: Path, key: bytes) -> dict[str, Any]:
    if len(key) < MIN_KEY_LENGTH:
        raise IngestionError(f"Tokenization key must be at least {MIN_KEY_LENGTH} bytes")
    frames, reports, hashes = [], {}, {}
    for path in raw_paths:
        raw = read_raw(path)
        reports[path.name] = quality_report(raw)
        try:
            RawTransactionSchema.validate(raw, lazy=True)
        except pandera.errors.SchemaErrors as exc:
            raise IngestionError(
                f"{path.name} failed schema validation:\n{exc.failure_cases.head(20)}"
            ) from exc
        hashes[path.name] = sha256_file(path)
        frames.append(canonicalize(raw, key, path.name))

    df = pd.concat(frames, ignore_index=True)
    dup = df["event_id"].duplicated(keep=False)
    if dup.any():
        # Exact duplicates (e.g. a re-delivered event) are dropped; conflicting duplicates (same id,
        # different content) indicate a data integrity problem and stop ingestion.
        conflicting = df[dup].drop(columns="source_file").drop_duplicates()
        if conflicting["event_id"].duplicated().any():
            raise IngestionError("Conflicting records share an event_id")
        df = df.drop_duplicates(subset="event_id", keep="first")
    df = df.sort_values(["event_time", "event_id"], kind="mergesort").reset_index(drop=True)
    CanonicalTransactionSchema.validate(df, lazy=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "card_transactions.parquet"
    df.to_parquet(data_path, index=False)

    version_src = json.dumps({"inputs": hashes, "ingestion": INGESTION_VERSION}, sort_keys=True)
    manifest = {
        "dataset": "sparkov_card_transactions",
        "domain": DOMAIN,
        "dataset_version": hashlib.sha256(version_src.encode()).hexdigest()[:12],
        "ingestion_version": INGESTION_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "inputs_sha256": hashes,
        "output": {
            "path": data_path.name,
            "sha256": sha256_file(data_path),
            "rows": len(df),
            "dropped_duplicate_rows": int(dup.sum() // 2) if dup.any() else 0,
            "fraud_count": int(df["is_fraud"].sum()),
            "prevalence": float(df["is_fraud"].mean()),
            "time_range": [df["event_time"].min().isoformat(), df["event_time"].max().isoformat()],
            "customers": int(df["customer_id"].nunique()),
        },
        "dropped_raw_fields": "all except " + ", ".join(RAW_COLUMNS) + " (cc_num tokenized)",
    }
    (out_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2))
    (out_dir / "data_quality_report.json").write_text(json.dumps(reports, indent=2))
    return manifest


def load_key() -> bytes:
    key = os.environ.get("FRAUD_TOKENIZATION_KEY", "")
    if not key:
        raise IngestionError("FRAUD_TOKENIZATION_KEY is not set (see .env.example)")
    return key.encode()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=Path("ml/data/processed"))
    args = parser.parse_args(argv)
    manifest = ingest(args.raw, args.out, load_key())
    print(json.dumps(manifest["output"], indent=2))
    print(f"dataset_version={manifest['dataset_version']}")


if __name__ == "__main__":
    main()
