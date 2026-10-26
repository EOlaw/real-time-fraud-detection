"""Data quality checks and report for raw-to-canonical ingestion.

Every check has a severity:

* ``error``   - ingestion fails; no canonical output is written (the previous output, if any, is
                left untouched) and the report is written as ``data_quality_report.failed.json``.
* ``warning`` - ingestion succeeds and the warning is recorded in the report and manifest
                (``--fail-on-warning`` turns warnings into failures).

The report contains counts, distributions, column names, row indices and event ids only. Values
of sensitive raw columns (card number, and any column we do not read) are never written:
failure examples carry values only for the non-sensitive columns in ``REPORTABLE_VALUE_COLUMNS``.
"""

from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd
import pandera.errors

from fraud_ml.data.schema import RawTransactionSchema

REPORT_VERSION = "2"
# Values from these raw columns may appear in failure examples. Everything else is redacted.
REPORTABLE_VALUE_COLUMNS = frozenset({"category", "is_fraud", "amt", "state"})
MAX_EXAMPLES = 5

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class QualityThresholds:
    # Longest period with no events (any customer) before a coverage warning.
    max_time_gap_days: float = 2.0
    # Largest allowed ratio between per-input fraud prevalences before a label-shift warning.
    max_prevalence_ratio_between_inputs: float = 3.0
    # Amounts above this are kept (they are legitimate signal) but flagged for review.
    amount_review_threshold: float = 100_000.0
    # Training data must contain at least this many positives and negatives.
    min_examples_per_class: int = 1


@dataclass
class Check:
    id: str
    severity: Severity
    passed: bool
    message: str
    input: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _example_value(column: str | None, value: Any) -> Any:
    if column in REPORTABLE_VALUE_COLUMNS and value is not None and not pd.isna(value):
        return value.item() if hasattr(value, "item") else value
    return "[redacted]"


def schema_failures(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Run the raw Pandera schema lazily and summarize failures without sensitive values."""
    try:
        RawTransactionSchema.validate(df, lazy=True)
    except pandera.errors.SchemaErrors as exc:
        cases = exc.failure_cases
        out = []
        for (column, check), group in cases.groupby(["column", "check"], dropna=False, sort=True):
            col = None if pd.isna(column) else str(column)
            idx = group["index"].dropna()
            out.append(
                {
                    "column": col,
                    "check": str(check),
                    "failure_count": len(group),
                    "example_row_indices": [int(i) for i in idx.head(MAX_EXAMPLES)],
                    "example_values": [
                        _example_value(col, v) for v in group["failure_case"].head(MAX_EXAMPLES)
                    ],
                }
            )
        return out
    return []


def _time_coverage(times: pd.Series) -> dict[str, Any]:
    times = times.dropna()
    if times.empty:
        return {"min": None, "max": None}
    ordered = times.sort_values()
    gaps = ordered.diff().dt.total_seconds().dropna()
    days = pd.date_range(ordered.iloc[0].normalize(), ordered.iloc[-1].normalize(), freq="D")
    observed_days = ordered.dt.normalize().unique()
    return {
        "min": ordered.iloc[0].isoformat(),
        "max": ordered.iloc[-1].isoformat(),
        "span_days": (ordered.iloc[-1] - ordered.iloc[0]).total_seconds() / 86400,
        "calendar_days_without_events": int(days.difference(observed_days).size),
        "longest_gap_days": float(gaps.max() / 86400) if len(gaps) else 0.0,
    }


def _amount_profile(amount: pd.Series, review_threshold: float) -> dict[str, Any]:
    amt = amount.dropna()
    if amt.empty:
        return {}
    levels = (0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0)
    q = amt.quantile(list(levels))
    iqr = q[0.75] - q[0.25]
    outliers = int((amt > q[0.75] + 3 * iqr).sum())
    return {
        "quantiles": {f"p{round(level * 100):02d}": float(q[level]) for level in levels},
        "mean": float(amt.mean()),
        "iqr_outliers_3x": outliers,
        "iqr_outliers_3x_share": outliers / len(amt),
        "above_review_threshold": int((amt > review_threshold).sum()),
        "policy": "outliers are reported and retained, never removed",
    }


def profile_input(df: pd.DataFrame, thresholds: QualityThresholds) -> dict[str, Any]:
    """Distributions for one normalized raw input (sensitive columns are only counted)."""
    label = df["is_fraud"]
    labeled = label.dropna()
    hours = df["trans_date_trans_time"].dropna().dt.hour.value_counts().sort_index()
    by_cat = df.groupby("category", dropna=True)["is_fraud"].agg(["count", "sum"])
    by_month = df.groupby(df["trans_date_trans_time"].dt.to_period("M"), dropna=True)[
        "is_fraud"
    ].agg(["count", "sum"])
    fraud = df["is_fraud"] == 1
    return {
        "rows": len(df),
        "missing_values": {c: int(n) for c, n in df.isna().sum().items()},
        "distinct_customers": int(df["cc_num"].nunique()),
        "distinct_regions": int(df["state"].nunique()),
        "time_coverage": _time_coverage(df["trans_date_trans_time"]),
        "labels": {
            "fraud": int((labeled == 1).sum()),
            "legitimate": int((labeled == 0).sum()),
            "prevalence": float((labeled == 1).mean()) if len(labeled) else None,
        },
        "amount": _amount_profile(df["amt"], thresholds.amount_review_threshold),
        "amount_by_label": {
            "fraud_median": float(df.loc[fraud, "amt"].median()) if fraud.any() else None,
            "legitimate_median": float(df.loc[~fraud, "amt"].median()) if (~fraud).any() else None,
        },
        "category": {
            str(k): {"rows": int(r["count"]), "fraud": int(r["sum"])} for k, r in by_cat.iterrows()
        },
        "hour_of_day_rows": {int(h): int(n) for h, n in hours.items()},
        "monthly": {
            str(p): {"rows": int(r["count"]), "fraud": int(r["sum"])}
            for p, r in by_month.iterrows()
        },
    }


@dataclass
class QualityReport:
    thresholds: QualityThresholds
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    combined: dict[str, Any] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    def add(
        self,
        id: str,
        severity: Severity,
        passed: bool,
        message: str,
        input: str | None = None,
        **details: Any,
    ) -> Check:
        check = Check(id, severity, passed, message, input, details)
        self.checks.append(check)
        return check

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "error" and not c.passed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "warning" and not c.passed]

    @property
    def status(self) -> str:
        if self.errors:
            return "failed"
        return "passed_with_warnings" if self.warnings else "passed"

    def to_dict(self, **extra: Any) -> dict[str, Any]:
        return {
            "report_version": REPORT_VERSION,
            "status": self.status,
            **extra,
            "summary": {
                "errors": [c.id + (f"[{c.input}]" if c.input else "") for c in self.errors],
                "warnings": [c.id + (f"[{c.input}]" if c.input else "") for c in self.warnings],
            },
            "thresholds": asdict(self.thresholds),
            "checks": [asdict(c) for c in self.checks],
            "inputs": self.inputs,
            "combined": self.combined,
        }


def check_labels(report: QualityReport, label: pd.Series) -> None:
    pos, neg = int((label == 1).sum()), int((label == 0).sum())
    need = report.thresholds.min_examples_per_class
    report.add(
        "labels.both_classes_present",
        "error",
        pos >= need and neg >= need,
        f"{pos} fraud and {neg} legitimate rows (need >= {need} of each)",
        fraud=pos,
        legitimate=neg,
    )


def check_prevalence_consistency(report: QualityReport) -> None:
    prevalences = {
        name: p["labels"]["prevalence"]
        for name, p in report.inputs.items()
        if p.get("labels", {}).get("prevalence")
    }
    if len(prevalences) < 2:
        return
    ratio = max(prevalences.values()) / min(prevalences.values())
    limit = report.thresholds.max_prevalence_ratio_between_inputs
    report.add(
        "labels.prevalence_consistent_across_inputs",
        "warning",
        ratio <= limit,
        f"max/min per-input fraud prevalence ratio {ratio:.2f} (limit {limit:g})",
        ratio=ratio,
        prevalences=prevalences,
    )


def check_time(report: QualityReport, times: pd.Series) -> None:
    coverage = _time_coverage(times)
    report.combined["time_coverage"] = coverage
    if coverage["min"] is None:
        return
    limit = report.thresholds.max_time_gap_days
    report.add(
        "time.no_large_gaps",
        "warning",
        coverage["longest_gap_days"] <= limit,
        f"longest period without events {coverage['longest_gap_days']:.2f} days (limit {limit:g})",
        longest_gap_days=coverage["longest_gap_days"],
    )
    ranges = [
        (name, p["time_coverage"]["min"], p["time_coverage"]["max"])
        for name, p in report.inputs.items()
        if p.get("time_coverage", {}).get("min")
    ]
    ranges.sort(key=lambda r: r[1])
    overlaps = [[a[0], b[0]] for a, b in itertools.pairwise(ranges) if b[1] <= a[2]]
    if len(ranges) > 1:
        report.add(
            "time.inputs_do_not_overlap",
            "warning",
            not overlaps,
            f"{len(overlaps)} pair(s) of input files overlap in time "
            "(overlapping train/test files can leak future data)",
            overlapping_inputs=overlaps,
        )


def check_amounts(report: QualityReport, amount: pd.Series) -> None:
    limit = report.thresholds.amount_review_threshold
    above = int((amount > limit).sum())
    report.add(
        "amount.below_review_threshold",
        "warning",
        above == 0,
        f"{above} row(s) above {limit:g}; retained, flagged for review",
        rows=above,
    )


def duplicate_analysis(
    raw: pd.DataFrame, key: str, compare: list[str]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Split duplicate ids into exact duplicates (dropped, keep first) and conflicts (same id,
    different content). Returns the de-duplicated frame and a summary with example ids only."""
    dup = raw[key].duplicated(keep=False)
    if not dup.any():
        return raw, {"duplicate_id_rows": 0, "exact_duplicates_dropped": 0, "conflicting_ids": 0}
    distinct = raw.loc[dup, compare].drop_duplicates()
    conflict_ids = distinct.loc[distinct[key].duplicated(keep=False), key].unique()
    deduped = raw.drop_duplicates(subset=compare, keep="first")
    return deduped, {
        "duplicate_id_rows": int(dup.sum()),
        "exact_duplicates_dropped": int(len(raw) - len(deduped)),
        "conflicting_ids": len(conflict_ids),
        "example_conflicting_ids": [str(i) for i in conflict_ids[:MAX_EXAMPLES]],
    }


def describe_failures(checks: list[Check]) -> str:
    return "; ".join(f"{c.id}{f' [{c.input}]' if c.input else ''}: {c.message}" for c in checks)
