from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fraud_ml.data import ingest as ingest_module
from fraud_ml.data.ingest import (
    DATA_FILE,
    FAILED_REPORT_FILE,
    MANIFEST_FILE,
    REPORT_FILE,
    IngestionError,
    ingest,
    sha256_file,
)
from fraud_ml.data.schema import CanonicalTransactionSchema
from tests.fixtures import KEY, PAN_A, PAN_B, SENSITIVE, raw_frame, write


def report_of(out: Path, failed: bool = False) -> dict[str, Any]:
    return json.loads((out / (FAILED_REPORT_FILE if failed else REPORT_FILE)).read_text())


def check(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    matches = [c for c in report["checks"] if c["id"] == check_id]
    assert matches, f"check {check_id} not in report"
    return matches[0]


def canonical(out: Path) -> pd.DataFrame:
    return pd.read_parquet(out / DATA_FILE)


# ---------------------------------------------------------------- happy path


def test_success_writes_outputs_and_complete_report(tmp_path: Path) -> None:
    src = write(raw_frame(), tmp_path / "raw.csv")
    manifest = ingest([src], tmp_path / "out", KEY)
    out = tmp_path / "out"
    df = canonical(out)
    CanonicalTransactionSchema.validate(df)
    assert not (out / FAILED_REPORT_FILE).exists()

    report = report_of(out)
    assert report["status"] == "passed"
    assert report["summary"] == {"errors": [], "warnings": []}
    assert {c["id"] for c in report["checks"]} >= {
        "input.readable",
        "input.required_columns",
        "input.non_empty",
        "schema.valid",
        "duplicates.no_conflicting_ids",
        "duplicates.no_exact_duplicates",
        "labels.both_classes_present",
        "time.no_large_gaps",
        "amount.below_review_threshold",
        "canonical.contract",
    }
    info = report["inputs"]["raw.csv"]
    assert info["format"] == "csv"
    assert info["rows"] == 8
    assert info["labels"] == {"fraud": 2, "legitimate": 6, "prevalence": 0.25}
    assert info["ignored_columns"] == ["Unnamed: 0", "dob", "first", "last", "street"]
    assert info["time_coverage"]["min"] == "2019-01-01T00:00:00"
    assert set(info["amount"]["quantiles"]) == {"p00", "p01", "p25", "p50", "p75", "p99", "p100"}
    assert info["category"]["grocery_pos"] == {"rows": 2, "fraud": 1}
    assert report["combined"]["rows_out"] == 8
    assert report["dataset_version"] == manifest["dataset_version"]

    assert manifest["quality"] == {"status": "passed", "warnings": []}
    assert manifest["output"]["sha256"] == sha256_file(out / DATA_FILE)
    assert manifest["input_formats"] == {"raw.csv": "csv"}
    assert df.loc[df["category"] == "shopping_net", "channel"].unique().tolist() == [
        "card_not_present"
    ]
    assert not df["merchant_id"].str.startswith("fraud_").any()


def test_output_is_point_in_time_sorted_and_independent_of_input_order(tmp_path: Path) -> None:
    raw = raw_frame()
    ingest([write(raw, tmp_path / "a.csv")], tmp_path / "a", KEY)
    shuffled = raw.sample(frac=1.0, random_state=3)
    ingest([write(shuffled, tmp_path / "b.csv")], tmp_path / "b", KEY)
    a, b = canonical(tmp_path / "a"), canonical(tmp_path / "b")
    pd.testing.assert_frame_equal(a.drop(columns="source_file"), b.drop(columns="source_file"))
    keys = list(zip(a["event_time"], a["event_id"], strict=True))
    assert keys == sorted(keys)


@pytest.mark.parametrize("variant", ["string_columns", "natural_types", "tz_aware_utc"])
def test_csv_and_parquet_produce_identical_canonical_output(tmp_path: Path, variant: str) -> None:
    raw = raw_frame()
    ingest([write(raw, tmp_path / "raw.csv")], tmp_path / "csv", KEY)
    pq_path = tmp_path / "raw.parquet"
    if variant == "tz_aware_utc":
        raw.assign(
            cc_num=raw["cc_num"].astype("int64"),
            trans_date_trans_time=pd.to_datetime(raw["trans_date_trans_time"]).dt.tz_localize(
                "UTC"
            ),
        ).to_parquet(pq_path, index=False)
    else:
        write(raw, pq_path, natural_types=variant == "natural_types")
    manifest = ingest([pq_path], tmp_path / "pq", KEY)

    from_csv, from_pq = canonical(tmp_path / "csv"), canonical(tmp_path / "pq")
    pd.testing.assert_frame_equal(
        from_csv.drop(columns="source_file"), from_pq.drop(columns="source_file")
    )
    assert manifest["input_formats"] == {"raw.parquet": "parquet"}


# ---------------------------------------------------------------- sensitive data


def _assert_no_sensitive(text: str) -> None:
    for value in SENSITIVE:
        assert value not in text, f"sensitive value leaked: {value!r}"


@pytest.mark.parametrize("scenario", ["success", "schema_failure", "conflict"])
def test_sensitive_raw_values_never_appear_in_outputs_reports_or_logs(
    tmp_path: Path,
    scenario: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = raw_frame()
    inputs = [write(raw, tmp_path / "a.csv")]
    if scenario == "schema_failure":
        raw.loc[0, "cc_num"] = "4111-1111-1111-1111"  # fails the digit pattern
        raw.loc[1, "category"] = "crypto"
        inputs = [write(raw, tmp_path / "a.csv")]
    elif scenario == "conflict":
        other = raw.iloc[:1].copy()
        other["cc_num"] = PAN_B  # same event id, different card -> conflict
        inputs.append(write(other, tmp_path / "b.csv"))

    monkeypatch.setenv("FRAUD_TOKENIZATION_KEY", KEY.decode())
    caplog.set_level(logging.DEBUG)
    argv = [arg for p in inputs for arg in ("--raw", str(p))] + ["--out", str(tmp_path / "out")]
    if scenario == "success":
        ingest_module.main(argv)
    else:
        with pytest.raises(SystemExit) as exc:
            ingest_module.main(argv)
        assert exc.value.code == 1

    captured = capsys.readouterr()
    _assert_no_sensitive(captured.out + captured.err + caplog.text)
    for path in (tmp_path / "out").iterdir():
        _assert_no_sensitive(path.read_bytes().decode("latin-1"))
        if path.suffix == ".parquet":
            _assert_no_sensitive(pd.read_parquet(path).to_csv())

    if scenario == "schema_failure":
        failures = check(report_of(tmp_path / "out", failed=True), "schema.valid")["details"][
            "failures"
        ]
        by_column = {f["column"]: f for f in failures}
        assert by_column["cc_num"]["example_values"] == ["[redacted]"]
        assert by_column["category"]["example_values"] == ["crypto"]  # non-sensitive: shown


# ---------------------------------------------------------------- duplicates


def test_exact_duplicates_within_and_across_files_are_dropped_with_warning(
    tmp_path: Path,
) -> None:
    raw = raw_frame()
    within = pd.concat([raw, raw.iloc[[3]]], ignore_index=True)
    a = write(within, tmp_path / "a.csv")
    b = write(raw.iloc[:2], tmp_path / "b.csv")
    manifest = ingest([a, b], tmp_path / "out", KEY)
    report = report_of(tmp_path / "out")
    assert report["status"] == "passed_with_warnings"
    assert "duplicates.no_exact_duplicates" in report["summary"]["warnings"]
    assert report["combined"]["duplicates"]["exact_duplicates_dropped"] == 3
    assert manifest["output"]["rows"] == 8
    assert manifest["output"]["dropped_duplicate_rows"] == 3
    # First occurrence wins, so the rows keep the first file's provenance.
    assert set(canonical(tmp_path / "out")["source_file"]) == {"a.csv"}


@pytest.mark.parametrize("where", ["within_file", "across_files"])
def test_conflicting_duplicate_ids_fail_ingestion(tmp_path: Path, where: str) -> None:
    raw = raw_frame()
    conflict = raw.iloc[[0]].assign(amt=999.0)
    if where == "within_file":
        inputs = [write(pd.concat([raw, conflict]), tmp_path / "a.csv")]
    else:
        inputs = [write(raw, tmp_path / "a.csv"), write(conflict, tmp_path / "b.csv")]
    with pytest.raises(IngestionError, match=r"duplicates\.no_conflicting_ids"):
        ingest(inputs, tmp_path / "out", KEY)
    report = report_of(tmp_path / "out", failed=True)
    assert report["status"] == "failed"
    details = check(report, "duplicates.no_conflicting_ids")["details"]
    assert details["conflicting_ids"] == 1
    assert details["example_conflicting_ids"] == [f"{0:032x}"]
    assert not (tmp_path / "out" / DATA_FILE).exists()


# ---------------------------------------------------------------- error conditions


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("amt", -5.0),
        ("amt", 10.123),  # more than 2 decimal places: never silently rounded
        ("category", "crypto"),
        ("trans_num", "not-hex"),
        ("cc_num", "abc"),
        ("state", "ny"),
        ("is_fraud", 2),
    ],
)
def test_schema_violations_fail(tmp_path: Path, column: str, value: object) -> None:
    raw = raw_frame()
    raw[column] = raw[column].astype(object)
    raw.loc[0, column] = value
    with pytest.raises(IngestionError, match=r"schema\.valid"):
        ingest([write(raw, tmp_path / "raw.csv")], tmp_path / "out", KEY)
    failures = check(report_of(tmp_path / "out", failed=True), "schema.valid")["details"][
        "failures"
    ]
    assert column in {f["column"] for f in failures}
    assert all(f["failure_count"] >= 1 for f in failures)


def test_unparseable_values_are_counted_not_crashing(tmp_path: Path) -> None:
    raw = raw_frame()
    raw.loc[0, "trans_date_trans_time"] = "not-a-date"
    raw["amt"] = raw["amt"].astype(object)
    raw.loc[1, "amt"] = "abc"
    with pytest.raises(IngestionError, match="2 unparseable value"):
        ingest([write(raw, tmp_path / "raw.csv")], tmp_path / "out", KEY)
    details = check(report_of(tmp_path / "out", failed=True), "schema.valid")["details"]
    assert details["unparseable_values"] == {"trans_date_trans_time": 1, "amt": 1}


def test_missing_required_column(tmp_path: Path) -> None:
    src = write(raw_frame().drop(columns="amt"), tmp_path / "raw.csv")
    with pytest.raises(IngestionError, match=r"input\.required_columns"):
        ingest([src], tmp_path / "out", KEY)
    details = check(report_of(tmp_path / "out", failed=True), "input.required_columns")["details"]
    assert details["missing"] == ["amt"]


@pytest.mark.parametrize(
    ("name", "content"), [("raw.json", b"{}"), ("raw.parquet", b"not parquet 4111111111111111")]
)
def test_unreadable_inputs_fail_without_echoing_content(
    tmp_path: Path, name: str, content: bytes
) -> None:
    (tmp_path / name).write_bytes(content)
    with pytest.raises(IngestionError, match=r"input\.readable") as exc:
        ingest([tmp_path / name], tmp_path / "out", KEY)
    assert PAN_A not in str(exc.value)
    assert PAN_A not in (tmp_path / "out" / FAILED_REPORT_FILE).read_text()


def test_empty_input_fails(tmp_path: Path) -> None:
    src = write(raw_frame().iloc[:0], tmp_path / "raw.csv")
    with pytest.raises(IngestionError, match=r"input\.non_empty"):
        ingest([src], tmp_path / "out", KEY)


def test_single_class_labels_fail(tmp_path: Path) -> None:
    src = write(raw_frame().assign(is_fraud=0), tmp_path / "raw.csv")
    with pytest.raises(IngestionError, match=r"labels\.both_classes_present"):
        ingest([src], tmp_path / "out", KEY)


def test_failure_leaves_previous_outputs_untouched(tmp_path: Path) -> None:
    out = tmp_path / "out"
    ingest([write(raw_frame(), tmp_path / "good.csv")], out, KEY)
    before = {p.name: sha256_file(p) for p in (out / DATA_FILE, out / MANIFEST_FILE)}

    bad = raw_frame().assign(category="crypto")
    with pytest.raises(IngestionError):
        ingest([write(bad, tmp_path / "bad.csv")], out, KEY)
    assert {p.name: sha256_file(p) for p in (out / DATA_FILE, out / MANIFEST_FILE)} == before
    assert (out / FAILED_REPORT_FILE).exists()
    assert not [p for p in out.iterdir() if p.name.startswith(".")]  # no staging leftovers

    ingest([write(raw_frame(), tmp_path / "good.csv")], out, KEY)
    assert not (out / FAILED_REPORT_FILE).exists()  # stale failure report removed on success


# ---------------------------------------------------------------- warnings


def test_large_amounts_are_retained_and_flagged(tmp_path: Path) -> None:
    raw = raw_frame()
    raw.loc[3, "amt"] = 25_000.00
    raw.loc[5, "amt"] = 150_000.00  # above the review threshold
    ingest([write(raw, tmp_path / "raw.csv")], tmp_path / "out", KEY)
    report = report_of(tmp_path / "out")
    assert report["summary"]["warnings"] == ["amount.below_review_threshold"]
    amounts = canonical(tmp_path / "out")["amount"].tolist()
    assert 25_000.00 in amounts and 150_000.00 in amounts
    profile = report["inputs"]["raw.csv"]["amount"]
    # With 8 rows, Q3 is pulled up by 25,000, so only 150,000 exceeds Q3 + 3*IQR.
    assert profile["iqr_outliers_3x"] == 1
    assert profile["quantiles"]["p100"] == 150_000.00
    assert profile["above_review_threshold"] == 1


def test_time_gap_overlap_and_prevalence_shift_warnings(tmp_path: Path) -> None:
    a = raw_frame()
    b = raw_frame(start="2019-01-10 00:00:00").assign(
        trans_num=[f"{i + 100:032x}" for i in range(8)],
        is_fraud=[1, 1, 1, 1, 1, 1, 1, 0],
    )
    overlap = raw_frame(start="2019-01-01 00:03:00").assign(
        trans_num=[f"{i + 200:032x}" for i in range(8)]
    )
    inputs = [
        write(a, tmp_path / "a.csv"),
        write(b, tmp_path / "b.csv"),
        write(overlap, tmp_path / "c.csv"),
    ]
    ingest(inputs, tmp_path / "out", KEY)
    warnings = report_of(tmp_path / "out")["summary"]["warnings"]
    assert "time.no_large_gaps" in warnings
    assert "time.inputs_do_not_overlap" in warnings
    assert "labels.prevalence_consistent_across_inputs" in warnings


def test_fail_on_warning(tmp_path: Path) -> None:
    raw = raw_frame()
    src = write(pd.concat([raw, raw.iloc[[0]]]), tmp_path / "raw.csv")
    with pytest.raises(IngestionError, match=r"duplicates\.no_exact_duplicates"):
        ingest([src], tmp_path / "out", KEY, fail_on_warning=True)
    assert report_of(tmp_path / "out", failed=True)["status"] == "passed_with_warnings"
    assert not (tmp_path / "out" / DATA_FILE).exists()


# ---------------------------------------------------------------- tokenization / config


def test_tokens_are_deterministic_and_key_dependent(tmp_path: Path) -> None:
    src = write(raw_frame(), tmp_path / "raw.csv")
    ingest([src], tmp_path / "a", KEY)
    ingest([src], tmp_path / "b", KEY)
    ingest([src], tmp_path / "c", b"z" * 32)
    a, b, c = (canonical(tmp_path / d) for d in "abc")
    assert a["customer_id"].tolist() == b["customer_id"].tolist()
    assert a["customer_id"].nunique() == 2
    assert set(a["customer_id"]).isdisjoint(set(c["customer_id"]))


def test_short_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="at least"):
        ingest([write(raw_frame(), tmp_path / "raw.csv")], tmp_path / "out", b"short")


def test_duplicate_input_names_rejected(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    a = write(raw_frame(), tmp_path / "raw.csv")
    b = write(raw_frame(), tmp_path / "x" / "raw.csv")
    with pytest.raises(IngestionError, match="unique"):
        ingest([a, b], tmp_path / "out", KEY)


# ---------------------------------------------------------------- real dataset (optional)

PROCESSED = Path("ml/data/processed/card_transactions.parquet")


@pytest.mark.dataset
@pytest.mark.skipif(not PROCESSED.exists(), reason="processed dataset not built")
def test_processed_dataset_contract() -> None:
    df = pd.read_parquet(PROCESSED)
    CanonicalTransactionSchema.validate(df)
    manifest = json.loads((PROCESSED.parent / MANIFEST_FILE).read_text())
    assert manifest["output"]["rows"] == len(df)
    assert manifest["output"]["sha256"] == sha256_file(PROCESSED)
    keys = df[["event_time", "event_id"]]
    assert keys.equals(keys.sort_values(["event_time", "event_id"], kind="mergesort"))
