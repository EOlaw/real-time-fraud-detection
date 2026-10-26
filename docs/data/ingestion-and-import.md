# Ingestion and PostgreSQL import (Phase 2)

Two steps take raw data to the database:

```text
raw CSV / Parquet ──ingest──▶ ml/data/processed/ ──import-transactions──▶ PostgreSQL transactions
                    (validate, minimize,    card_transactions.parquet      (one tenant, labeled
                     tokenize, dedupe,      data_quality_report.json        source, idempotent,
                     sort, version)         dataset_manifest.json           all-or-nothing)
```

Code: [`ml/fraud_ml/data/ingest.py`](../../ml/fraud_ml/data/ingest.py),
[`ml/fraud_ml/data/quality.py`](../../ml/fraud_ml/data/quality.py),
[`apps/api/fraud_api/db/importer.py`](../../apps/api/fraud_api/db/importer.py),
[`apps/api/fraud_api/cli.py`](../../apps/api/fraud_api/cli.py).

## 1. Ingestion

```bash
make ingest                      # the two Sparkov files, default RAW paths
# or explicitly (CSV and Parquet inputs can be mixed):
set -a; source .env; set +a      # FRAUD_TOKENIZATION_KEY (>= 32 chars)
uv run python -m fraud_ml.data.ingest \
    --raw ml/data/raw/sparkov/credit_card_transaction_train.csv \
    --raw ml/data/raw/sparkov/credit_card_transaction_test.csv \
    --out ml/data/processed [--fail-on-warning]
```

| Exit | Meaning | Files written to `--out` |
|---|---|---|
| 0 | Passed (possibly with warnings) | `card_transactions.parquet`, `data_quality_report.json`, `dataset_manifest.json` (replaced; any stale `data_quality_report.failed.json` is deleted) |
| 1 | An error check failed (or a warning with `--fail-on-warning`) | only `data_quality_report.failed.json`; existing outputs are left untouched |

Outputs are built in a hidden staging directory inside `--out` and moved into place file by file,
with the manifest last. A manifest whose `output.sha256` matches the parquet therefore marks a
complete run; the importer refuses parquet that does not match its manifest.

### Input formats

Only the eight required raw columns are ever read (`trans_date_trans_time, cc_num, merchant,
category, amt, state, trans_num, is_fraud`). Every other column (names, street, DOB, gender, job,
coordinates, …) is listed by *name* in the report's `ignored_columns` and never loaded.

Both formats are normalized to the same types before validation:

| Column | CSV | Parquet |
|---|---|---|
| `trans_date_trans_time` | ISO-8601 text | text, naive timestamp, or tz-aware timestamp (converted to UTC) |
| `cc_num` | text | text or integer; float only while exactly representable (≤ 2^53) |
| `amt`, `is_fraud` | text → numeric | numeric or text |

Unparseable values become nulls and are counted per column (`unparseable_values`); they fail the
`schema.valid` check instead of crashing ingestion.

**Verified equivalence:** the real `credit_card_transaction_test.csv` (555,719 rows) and a Parquet
copy with integer card numbers and native timestamps produced identical canonical frames
(`pandas.testing.assert_frame_equal`, excluding `source_file`, which records the input file name).
Unit tests cover text-typed, natively typed and tz-aware Parquet variants.

### Quality checks: errors vs. warnings

| Check id | Severity | Fails when |
|---|---|---|
| `input.readable` | error | unsupported extension or unreadable file (only the exception *type* is recorded) |
| `input.required_columns` | error | any of the eight required columns is missing |
| `input.non_empty` | error | zero data rows |
| `schema.valid` | error | any row violates the raw schema: null, card number not 8–19 digits, `trans_num` not 32 hex chars, unknown category, state not 2 upper-case letters, label not 0/1, amount ≤ 0, ≥ 1,000,000 or with more than 2 decimal places; or any unparseable value |
| `duplicates.no_conflicting_ids` | error | the same `trans_num` appears with different content (within or across files) |
| `labels.both_classes_present` | error | fewer than 1 fraud or 1 legitimate row |
| `canonical.contract` | error | the canonical output fails its own schema (internal safeguard) |
| `duplicates.no_exact_duplicates` | warning | identical rows were found; the first is kept, the rest dropped and counted |
| `labels.prevalence_consistent_across_inputs` | warning | per-file fraud prevalence differs by more than 3× (label shift) |
| `time.no_large_gaps` | warning | more than 2 days with no events at all |
| `time.inputs_do_not_overlap` | warning | input files overlap in time (train/test leakage risk) |
| `amount.below_review_threshold` | warning | amounts above 100,000 are present |

Thresholds live in `QualityThresholds` and are written into every report.

**Outliers are never removed.** Large amounts are legitimate signal (fraud is heavily concentrated
there: on the real training file the median fraud amount is $396.51 vs $47.28 for legitimate
rows). The report counts 3×IQR outliers (29,813 rows, 2.3% of the training file) and keeps them.

### Report format (`report_version: "2"`)

```json
{
  "report_version": "2",
  "status": "passed | passed_with_warnings | failed",
  "generated_at": "...", "ingestion_version": "2", "canonical_schema_version": "1",
  "dataset_version": "ef8be78c6f87",            // success only
  "summary": {"errors": ["schema.valid[raw.csv]"], "warnings": []},
  "thresholds": {...},
  "checks": [{"id", "severity", "passed", "message", "input", "details"}],
  "inputs": {
    "<file name>": {
      "format", "sha256", "ignored_columns", "rows", "missing_values",
      "distinct_customers", "distinct_regions",
      "time_coverage": {"min", "max", "span_days", "calendar_days_without_events",
                        "longest_gap_days"},
      "labels": {"fraud", "legitimate", "prevalence"},
      "amount": {"quantiles": {"p00".."p100"}, "mean", "iqr_outliers_3x",
                 "iqr_outliers_3x_share", "above_review_threshold"},
      "amount_by_label", "category": {"<cat>": {"rows", "fraud"}},
      "hour_of_day_rows", "monthly": {"YYYY-MM": {"rows", "fraud"}}
    }
  },
  "combined": {"duplicates": {"duplicate_id_rows", "exact_duplicates_dropped",
               "conflicting_ids", "example_conflicting_ids"},
               "time_coverage", "rows_in", "rows_out", "customers", "fraud", "prevalence",
               "channel_rows"}
}
```

Schema failures are summarized as `{column, check, failure_count, example_row_indices,
example_values}`. Row indices are 0-based data rows (header excluded). `example_values` are shown
only for `category`, `is_fraud`, `amt` and `state`; every other column, including `cc_num`, shows
`"[redacted]"`. Conflict examples list `trans_num` event ids only.

**Real-data result (2026-10-26):** both Sparkov files `passed` with no warnings: 1,852,394 rows
in and out, 0 duplicate ids, 0 conflicts, 999 customers, prevalence 0.521%, coverage
2019-01-01 → 2020-12-31 with one calendar day without events (longest gap 1.0 day), no amount above
100,000 (max 28,948.90). Ingestion took 11 s on an M4 Pro.

### Manifest and dataset version

`dataset_manifest.json` adds `ingestion_version` (code, now `"2"`), `canonical_schema_version`
(contract, `"1"`), `input_formats`, `quality.{status, warnings}` and `output.sort_order`.
The dataset version is a hash of the input file hashes plus the *canonical contract* version, so
it identifies the data rather than the code. Ingestion v2 reproduced the Phase 1 output
byte-for-byte (parquet SHA-256 `d3e296de…`), so the dataset version is still `ef8be78c6f87` and
the Phase 1 model's lineage is unchanged.

### Point-in-time ordering

Canonical rows are sorted by `(event_time, event_id)` with a stable sort, regardless of input file
order or row order (tested by shuffling). Ties on `event_time` are ordered by `event_id`. Feature
computation and replay rely on this order.

### Sensitive data

* Card numbers are replaced by `HMAC-SHA256(key, PAN)` tokens before anything is written. Other raw
  personal columns are never read.
* Reports, the manifest, CLI stdout/stderr and log records contain counts, column names, row
  indices, event ids, file names and the redaction-safe examples above. Tests scan all of these,
  plus the decoded parquet, for card numbers, names, street and DOB in success, schema-failure and
  conflict runs.
* On the real data, no output column equals any raw card number, name, street or DOB, and no
  13–19-digit run in the canonical output equals a real card number. Note that the synthetic
  merchant names are built from common surnames (e.g. "Abbott-Rogahn"), so a naive substring scan
  for customer surnames finds matches inside `merchant_id`. These are merchant names from the
  source, not customer data.

## 2. Import into PostgreSQL

```bash
make db-up migrate
set -a; source .env; set +a
uv run python -m fraud_api.cli create-org --slug demo --name "Demo Bank (sandbox)"   # once
uv run python -m fraud_api.cli import-transactions --org demo \
    --data ml/data/processed/card_transactions.parquet --source backfill \
    [--start 2020-05-22T12:14:00+00:00] [--end 2020-06-21T12:14:00+00:00] \
    [--statement-timeout-s 600]
# or: make import ORG=demo SOURCE=backfill [START=...] [END=...]
```

`load-history` (the Phase 1 command) still works. It is now an alias for `import-transactions
--source backfill` with a required window, so it gains the same idempotency and conflict checks.

Output (stdout, exit 0):

```json
{
  "organization": "demo", "source": "backfill", "dataset_version": "ef8be78c6f87",
  "parquet_sha256": "d3e296de…", "window_start": "...", "window_end": "...",
  "rows_selected": 83971, "inserted": 0, "already_present": 83971
}
```

### Semantics

| Property | Behaviour |
|---|---|
| Input verification | `dataset_manifest.json` must sit next to the parquet, and the parquet's name and SHA-256 must match it; the canonical schema is re-validated; amounts must have ≤ 2 decimals |
| Window | optional `[start, end)`; timezone required |
| Tenant | rows are written for `--org` only; conflicts are checked only within that organization; the same ids may exist in other organizations |
| Provenance | `source` ∈ `backfill`, `replay`, `simulated` (`live` is refused: a file is never live traffic); `schema_version = 'canonical-1'`; `payload_sha256` = SHA-256 of the canonical fields |
| Idempotency | an existing `(organization, external_id)` row with identical customer, merchant, amount, currency, category, channel, region, event time and source counts as `already_present` |
| Conflicts | the same id with any different field, including a different `source`, aborts the import: exit 1, `import failed: N transaction id(s) already exist … (e.g. <ids>); nothing was imported` |
| Atomicity | per-tenant advisory lock → transaction-local statement timeout → `COPY` into a temp staging table → conflict check → `INSERT … ON CONFLICT DO NOTHING` → post-insert verification → audit record → single `COMMIT`. Any error rolls back the entire import |
| Concurrency | concurrent imports into one tenant are serialized by the advisory lock; a concurrent API write that races the check is caught by the post-insert verification |
| Labels | `is_fraud` is **not** imported (labels get provenance-tracked storage in Phase 8) |
| Audit | one `transactions.import` audit record per successful run with counts, source, window, dataset version and parquet hash |
| Scoring interaction | imported rows are unscored history used by online features. Scoring a `transaction_id` that already exists as imported history returns `409 transaction_exists_unscored` (previously an unhandled 500) |

Database errors (including a statement timeout) exit 1 with `import failed and was rolled back:
<ErrorType>: <first line>`. Only the first line is printed, because PostgreSQL `CONTEXT` lines can
echo row contents.

### Measured (2026-10-26, Apple M4 Pro, PostgreSQL 17.6 in Docker, same host)

| Run | Rows selected | Inserted | Already present | Wall time |
|---|---|---|---|---|
| Full dataset into an empty database | 1,852,394 | 1,852,394 | 0 | 58 s |
| Immediate retry | 1,852,394 | 0 | 1,852,394 | 40 s |
| Phase 1 `load-history` command re-run on the dev database (rows written by the Phase 1 loader) | 83,971 | 0 | 83,971 | — |

After the full import, the database held 1,852,394 rows, 999 customers, the same time range, and
`sum(amount)` = 129,785,332.01, equal to the parquet sum to the cent. The first attempt at this
run exposed the API engine's 5 s statement timeout cancelling the `COPY`. That failed attempt left
0 rows and 0 audit records, and is why imports now set their own transaction-local timeout.
