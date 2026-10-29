# Monitoring and metrics API (Phase 9)

Methods, thresholds and the meaning of each metric are documented in
[docs/ml/monitoring.md](../ml/monitoring.md). Operations (Prometheus, Grafana, alerts) are in
[docs/operations/monitoring.md](../operations/monitoring.md).

These endpoints are **read-only**. Monitoring runs are computed by the monitoring job
(`python -m fraud_api.monitoring.cli run`). Promotion and rollback happen only in the CLI, with an
operator name and an audit record. No HTTP endpoint computes drift, acknowledges runs, promotes or
retrains.

## `GET /api/v1/monitoring/runs`

**Roles:** admin, analyst, viewer. Service accounts get `403`.

**Query:** `limit` 1–100 (default 20).

**Returns:** monitoring runs of the caller's organization, newest window first.
**Only `scope = organization` runs of the caller's organization are returned.** Global runs
aggregate every tenant and are visible only through the operator CLI.

```json
[{
  "id": "7c0e…",
  "model": "card-fraud-xgb@1.0.0",
  "window_start": "2026-10-28T00:00:00Z",
  "window_end": "2026-10-29T00:00:00Z",
  "labels_as_of": "2026-10-28T17:00:00Z",
  "maturity_days": 30,
  "status": "alert",
  "recommendation": "retraining_review",
  "summary": {"assessments": 20000, "reference": {"window_start": "…", "events": 271832}, "…": "…"},
  "calc_version": "drift-v1",
  "finished_at": "2026-10-28T17:02:11Z",
  "acknowledged": true
}]
```

| Field | Values |
|---|---|
| `status` | `ok`, `warn`, `alert`, `insufficient_data`, `failed` (worst thresholded unlabeled metric on segment `all`) |
| `recommendation` | `none`, `review`, `retraining_review`. **Advisory only.** Nothing is promoted or retrained automatically. |
| `acknowledged` | An operator acknowledged the run with `monitoring.cli ack`. Acknowledgement is cleared when a rerun changes the status. |

## `GET /api/v1/monitoring/runs/{run_id}`

**Roles:** admin, analyst, viewer.

**Returns:** the run plus every metric. A run of another organization, or a global run, returns
**`404`**, not `403`, so the response does not reveal that the run exists.

```json
{
  "...run fields...": "",
  "metrics": [
    {"metric": "score_psi", "segment": "all", "value": 0.0056, "ci_low": 0.0037,
     "ci_high": 0.0080, "sample_size": 20000, "reference_size": 271832, "labeled": false,
     "status": "ok", "thresholds": {"warn": 0.1, "alert": 0.25, "eps": 0.0001},
     "details": {"bins": 11, "current_missing_rate": 0.0, "reference_missing_rate": 0.0}},
    {"metric": "precision_final", "segment": "all", "value": 0.4258, "ci_low": 0.3507,
     "ci_high": 0.5045, "sample_size": 20000, "reference_size": null, "labeled": true,
     "status": "ok", "thresholds": {"min_labels": 200, "min_positives": 20},
     "details": {"…": "…"}}
  ]
}
```

- **Unlabeled vs labeled:** `labeled = false` rows are unlabeled distribution and operational
  signals. `labeled = true` rows are label-matured performance on the labeled subset only.
- **Selection-bias caveat:** stored with each labeled metric and omitted here to keep responses
  small. The UI shows it with the labeled table; the text is in
  [docs/ml/monitoring.md](../ml/monitoring.md#label-matured-performance-labeled-metrics).
- **No identifiers in metrics:** no transaction, customer or case identifiers appear in any metric.
  `details` holds bin counts and other aggregates only.

## `GET /metrics` (Prometheus)

- **Not part of the versioned API**, and not in the OpenAPI schema.
- **Token required:** `Authorization: Bearer <FRAUD_METRICS_TOKEN>`. Anything else returns `401`.
  With no token configured the endpoint returns **`404`**.
- **Response:** Prometheus text format, with bounded labels only. The metric list and access
  model are in [docs/operations/monitoring.md](../operations/monitoring.md#metrics).
- **Health endpoints** (`/health/live`, `/health/ready`) are unchanged and separate.

## Error added in Phase 9

| Status | Code | When |
|---|---|---|
| 503 | `database_unavailable` | Connection-pool timeout or operational database error, with `Retry-After: 2`. Retry later; nothing was written. Before Phase 9 this was a 500. |
