from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fraud_ml.data.ingest import IngestionError, ingest
from fraud_ml.data.schema import CanonicalTransactionSchema

KEY = b"k" * 32
PAN = "4111111111111111"


def _raw(rows: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Unnamed: 0": range(rows),
            "trans_date_trans_time": [f"2019-01-01 00:0{i}:00" for i in range(rows)],
            "cc_num": [PAN, PAN, "5500000000000004", "5500000000000004"][:rows],
            "merchant": ["fraud_Acme"] * rows,
            "category": ["grocery_pos", "shopping_net", "travel", "home"][:rows],
            "amt": [10.5, 20.0, 300.0, 4.99][:rows],
            "first": ["Jane"] * rows,
            "last": ["Doe"] * rows,
            "street": ["1 Main St"] * rows,
            "dob": ["1980-01-01"] * rows,
            "state": ["NY"] * rows,
            "trans_num": [f"{i:032x}" for i in range(rows)],
            "is_fraud": [0, 1, 0, 0][:rows],
        }
    )


def test_ingest_minimizes_and_tokenizes(tmp_path: Path) -> None:
    src = tmp_path / "raw.csv"
    _raw().to_csv(src, index=False)
    manifest = ingest([src], tmp_path / "out", KEY)
    df = pd.read_parquet(tmp_path / "out" / "card_transactions.parquet")
    CanonicalTransactionSchema.validate(df)
    assert set(df.columns).isdisjoint({"cc_num", "first", "last", "street", "dob"})
    assert PAN not in df.to_csv()
    assert df["customer_id"].nunique() == 2
    assert df.loc[df["category"] == "shopping_net", "channel"].item() == "card_not_present"
    assert df.loc[df["category"] == "travel", "channel"].item() == "unspecified"
    assert not df["merchant_id"].str.startswith("fraud_").any()
    assert manifest["output"]["rows"] == 4
    report = json.loads((tmp_path / "out" / "data_quality_report.json").read_text())
    assert report["raw.csv"]["fraud_count"] == 1


def test_tokens_are_deterministic_and_key_dependent(tmp_path: Path) -> None:
    src = tmp_path / "raw.csv"
    _raw().to_csv(src, index=False)
    ingest([src], tmp_path / "a", KEY)
    ingest([src], tmp_path / "b", KEY)
    ingest([src], tmp_path / "c", b"z" * 32)
    a, b, c = (pd.read_parquet(tmp_path / d / "card_transactions.parquet") for d in "abc")
    assert a["customer_id"].tolist() == b["customer_id"].tolist()
    assert set(a["customer_id"]).isdisjoint(set(c["customer_id"]))


def test_exact_duplicate_events_are_dropped(tmp_path: Path) -> None:
    raw = _raw()
    (tmp_path / "a.csv").write_text(raw.to_csv(index=False))
    (tmp_path / "b.csv").write_text(raw.iloc[:2].to_csv(index=False))
    manifest = ingest([tmp_path / "a.csv", tmp_path / "b.csv"], tmp_path / "out", KEY)
    assert manifest["output"]["rows"] == 4
    assert manifest["output"]["dropped_duplicate_rows"] == 2


def test_conflicting_duplicates_fail(tmp_path: Path) -> None:
    raw = _raw()
    conflict = raw.iloc[:1].copy()
    conflict["amt"] = 999.0
    raw.to_csv(tmp_path / "a.csv", index=False)
    conflict.to_csv(tmp_path / "b.csv", index=False)
    with pytest.raises(IngestionError, match="Conflicting"):
        ingest([tmp_path / "a.csv", tmp_path / "b.csv"], tmp_path / "out", KEY)


@pytest.mark.parametrize(
    ("column", "value"),
    [("amt", -5.0), ("category", "crypto"), ("trans_num", "not-hex"), ("cc_num", "abc")],
)
def test_schema_violations_fail(tmp_path: Path, column: str, value: object) -> None:
    raw = _raw()
    raw[column] = raw[column].astype(object)
    raw.loc[0, column] = value
    raw.to_csv(tmp_path / "raw.csv", index=False)
    with pytest.raises(IngestionError, match="schema validation"):
        ingest([tmp_path / "raw.csv"], tmp_path / "out", KEY)


def test_short_key_rejected(tmp_path: Path) -> None:
    _raw().to_csv(tmp_path / "raw.csv", index=False)
    with pytest.raises(IngestionError, match="at least"):
        ingest([tmp_path / "raw.csv"], tmp_path / "out", b"short")


PROCESSED = Path("ml/data/processed/card_transactions.parquet")


@pytest.mark.dataset
@pytest.mark.skipif(not PROCESSED.exists(), reason="processed dataset not built")
def test_processed_dataset_contract() -> None:
    df = pd.read_parquet(PROCESSED)
    CanonicalTransactionSchema.validate(df)
    manifest = json.loads((PROCESSED.parent / "dataset_manifest.json").read_text())
    assert manifest["output"]["rows"] == len(df)
    assert df["event_time"].is_monotonic_increasing
