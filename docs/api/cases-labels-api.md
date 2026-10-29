# Cases and labels API (Phase 8)

Every endpoint requires a bearer token. The organization comes from the token, and another
tenant's case, assessment, transaction or user id is answered as `404` (or `422` for an assignee),
never as "forbidden". Errors use the standard envelope
`{"error": {"code", "message", "correlation_id", "details"?}}`.

| Method | Path | admin | analyst | viewer | service |
|---|---|---|---|---|---|
| GET | `/api/v1/cases`, `/api/v1/cases/{id}`, `/api/v1/cases/assignees` | ✓ | ✓ | ✓ | – |
| POST | `/api/v1/cases` (open) | ✓ | ✓ | – | – |
| POST | `/api/v1/cases/{id}/notes` | ✓ | ✓ | – | – |
| POST | `/api/v1/cases/{id}/assign` | ✓ anyone | claim unassigned / release own | – | – |
| POST | `/api/v1/cases/{id}/status` | ✓, incl. reopen | assignee only, no reopen | – | – |
| POST | `/api/v1/cases/{id}/resolve` | ✓ | assignee only | – | – |
| GET | `/api/v1/labels?transaction_id=` | ✓ | ✓ | ✓ | – |
| POST | `/api/v1/labels` | ✓ | ✓ | – | – |
| GET | `/api/v1/fraud/insights/cases`, `/labels`, `/performance` | ✓ | ✓ | ✓ | – |

**Database failures.** A database failure in any mutating endpoint returns
`503 database_unavailable` with `Retry-After`, and nothing is changed: each operation runs in one
database transaction.

**Effect on decisions.** Cases and labels never modify `risk_assessments`. Opening, resolving or
labeling cannot change the model probability, the policy decision or the final decision.

## Cases

### `POST /api/v1/cases`

Request: `{"assessment_id": "<uuid>", "note": "optional, ≤ 10000"}`.
- **New case:** `201` with `{"created": true, "case": CaseDetail}`.
- **Transaction already has a case:** `200` with `{"created": false, …}`. There is one case per
  transaction, and concurrent requests produce exactly one.
- **Assessment not in your organization:** `404`.

**Severity** comes from the assessment's risk level: `low`, `medium`, `high` and `critical` map
to themselves. A degraded assessment (risk level `unknown`, no score) maps to **`high`**, never
`low`.

### `GET /api/v1/cases`

Query parameters:

| Parameter | Values |
|---|---|
| `status` | repeatable case status |
| `severity` | repeatable |
| `assigned` | `any`, `me` or `unassigned` |
| `source` | repeatable: `live`, `replay`, `simulated`, `backfill` |
| `sort` | `opened_at`, `severity` or `updated_at` |
| `order` | `asc` or `desc` |
| `limit` | 1–200 |
| `offset` | ≥ 0 |

Response: `{total, limit, offset, items: [CaseSummary]}`. `CaseSummary` carries:
- **case fields:** `id`, `transaction_id`, `assessment_id`, `source`, `severity`, `status`,
  `assigned_to`, `opened_by`, `opened_at`, `resolved_at`, `updated_at`, `version`;
- **assessment fields:** `assessment_status`, `risk_score`, `policy_decision`, `decision`;
- **label:** `current_label`.

### `GET /api/v1/cases/{id}` → `CaseDetail`

`CaseSummary` plus:
- `transaction`: id, customer token, merchant, amount, category, channel, event time, source. No
  card numbers or region.
- `model`: `name`, `version`, `registry_status`, and `serving` (whether this API instance serves
  that version).
- `events`: status and assignment history.
- `notes`.
- `labels`: the full label history.
- `allowed_transitions`: the statuses this caller may move the case to now.

The decision trace and the SHAP explanation come from the existing
`GET /fraud/predictions/{assessment_id}` and `…/explanation` endpoints. The explanation returns
`409 not_explainable` for degraded assessments and `409 model_mismatch` for versions not served.

### Mutations and optimistic locking

Every case mutation except notes names the version it read: `{"expected_version": n, …}`.

* A stale version returns `409 version_conflict`, and nothing is written. The check runs before
  anything else, so a stale writer always learns that it is stale.
* The update itself is `UPDATE … WHERE version = n`. A database trigger requires the version to
  rise by exactly 1, forbids `DELETE` and changes to identity columns.
* Each change appends a `case_events` row and an `audit_logs` record in the same transaction:
  `case.create`, `case.assign`, `case.status`, `case.resolve`, `case.reopen`, `case.note`.

| Endpoint | Body | Notes |
|---|---|---|
| `…/assign` | `assignee_id` (uuid or `null`), `expected_version` | Assignee must be an active analyst or admin of the organization (`422 invalid_assignee`). Same assignee → `409 no_change`. Closed case → `409 case_closed`. |
| `…/status` | `status` (`under_review` or `escalated`), `expected_version` | Open statuses only. From a closed status, `under_review` reopens (admin only) and clears `resolved_at`. |
| `…/resolve` | `outcome` (`fraud`, `legitimate` or `inconclusive`), `expected_version`, `note?`, `record_label` (default false), `label_confidence` | Sets the status to `resolved_fraud`, `resolved_legitimate` or `inconclusive` and `resolved_at = now` in one update. With `record_label`, also appends an `analyst_review` label for this case; refused with `409 label_exists` (nothing changed) if the transaction already has a label. |
| `…/notes` | `body` (1–10000), `evidence` (≤ 20 references of ≤ 500 characters) | Append-only. Any analyst or admin; assignment not required. |

**Status machine:**
```
new → under_review | escalated
under_review ↔ escalated
under_review | escalated → resolved_fraud | resolved_legitimate | inconclusive   (resolve)
closed → under_review                                                          (reopen, admin)
```

- Anything else returns `409 invalid_transition`; this includes `new → resolved` (a case must be
  reviewed first), closed → closed, and anything → `new`.
- An analyst who is not the assignee gets `403 not_assignee`.
- The database checks that `resolved_at` is set exactly when the status is closed, and that it is
  not before `opened_at`.

## Labels

### `POST /api/v1/labels` → `201 LabelOut`

```json
{
  "transaction_id": "txn_000123",
  "label": "fraud",
  "source": "chargeback",
  "confidence": "confirmed",
  "labeled_at": "2026-10-20T09:00:00Z",
  "case_id": null,
  "supersedes_id": null,
  "reason": null
}
```

**Fields:**
- `label`: `fraud`, `legitimate` or `inconclusive`.
- `source`: `analyst_review` (requires a `case_id` for the same transaction), `chargeback` or
  `customer_report`. **`dataset` labels are rejected here**; they come only from the CLI import.
- `labeled_at`: when the outcome became known. Requires a timezone; may not be in the future
  (5-minute clock-skew allowance) or before the transaction's event time.
- `labeled_by` is taken from the token, never from the body.

**Supersession (append-only):**
- The first label of a transaction must not set `supersedes_id`.
- Any later label is a correction: it must set `supersedes_id` to the current label and give a
  `reason`, and it may not be dated before the label it replaces.
- Any other `supersedes_id`, and losing a race with a concurrent correction, return
  `409 label_conflict`.
- Unique indexes enforce one first label per transaction and one correction per label, and a
  trigger forbids `UPDATE` and `DELETE`.
- Audit actions: `label.create`, `label.supersede`; `label.import_dataset` for the CLI import.

**`LabelOut` fields:** `id`, `transaction_id`, `label`, `source`, `confidence`, `case_id`,
`labeled_by` (`{id, email}`, `null` only for dataset labels), `labeled_at`, `recorded_at`,
`supersedes_id`, `superseded_by`, `current`, `reason`, `provenance`.

### `GET /api/v1/labels?transaction_id=…` → `{transaction_id, current, history}`

History is oldest first and includes superseded labels.

### CLI: dataset labels

```bash
uv run python -m fraud_api.cli import-dataset-labels --org demo \
  --data ml/data/processed/card_transactions.parquet \
  --start 2020-06-21T12:14:00+00:00 --end 2020-06-22T00:00:00+00:00
```

- Records the synthetic `is_fraud` value as `source=dataset`, `confidence=confirmed`.
- Only transactions already stored for the organization are labeled.
- Transactions with any existing label are skipped: the import is idempotent and never
  overwrites analyst or chargeback labels.
- `labeled_at` is the import time, because the dataset has no label timing. The provenance
  records the column, the dataset version and that caveat.

## Analytics

These endpoints use the common window parameters (`start`, `end`, `basis`, `source`; see
[rules-analytics-api.md](rules-analytics-api.md)).

* **`/fraud/insights/cases`:** cases opened (`opened_at`) and resolved (`resolved_at`) in the
  window, by severity and outcome; p50 and p90 hours to resolution; open cases now by status, and
  how many are unassigned. `basis` does not apply.
* **`/fraud/insights/labels`:** label rows recorded with `labeled_at` in the window, corrections
  among them, and current labels by label, source and confidence.
* **`/fraud/insights/performance`:** label-matured performance. Extra parameters:
  - `as_of`: default now;
  - `maturity_days`: default `FRAUD_LABEL_MATURITY_DAYS = 30`;
  - `include_probable`: default `false`.

  The cohort is selected by **event time** (`basis` is ignored). The response returns:
  - **population counts:** immature, unlabeled, inconclusive and probable-excluded counts, usable
    labels by class and source, and coverage;
  - **confusion matrices:** for the final and for the policy decision, with rates as
    `{numerator, denominator, rate}`;
  - mean scores by label;
  - a separate `unlabeled_signals` block;
  - a `caveat` stating that the labeled subset is selected, not ground truth.

  Semantics: [model-development-plan.md §8](../ml/model-development-plan.md).
