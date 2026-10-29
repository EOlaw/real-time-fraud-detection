# Scoring and model metadata API (Phases 1 & 4)

Interactive OpenAPI: `/docs` (schema at `/openapi.json`, with request examples). All endpoints
below require `Authorization: Bearer <token>` from `POST /api/v1/auth/token`. Errors use the
envelope `{"error": {"code", "message", "correlation_id", "details"?}}`, and every response
carries `X-Request-ID`.

| Method | Path | Roles | Phase |
|---|---|---|---|
| POST | `/api/v1/fraud/score` | service, admin | 1 |
| POST | `/api/v1/fraud/batch` | service, admin | 4 |
| GET | `/api/v1/fraud/predictions/{assessment_id}` | admin, analyst, viewer, service | 1 |
| GET | `/api/v1/models` | admin, analyst, viewer, service | 4 |
| GET | `/api/v1/models/active` | admin, analyst, viewer, service | 4 |
| GET | `/api/v1/models/{model_version_id}` | admin, analyst, viewer, service | 4 |
| GET | `/api/v1/models/{model_version_id}/metrics` | admin, analyst, viewer, service | 4 |

Rules (`/api/v1/rules…`) and analytics (`/api/v1/fraud/insights/{overview,volume,rule-hits,latency}`)
endpoints, added in Phase 6, are specified in [rules-analytics-api.md](rules-analytics-api.md).

## `POST /api/v1/fraud/score` (single event)

Request (`ScoreRequest`; unknown fields are rejected):

```json
{
  "transaction_id": "txn_000123",
  "customer_id": "cus_3f9a0c1d2e4b5a67",
  "merchant_id": "Kirlin and Sons",
  "amount": "84.20",
  "currency": "USD",
  "category": "grocery_pos",
  "event_time": "2020-06-21T12:14:25Z",
  "region": "SC",
  "source": "replay"
}
```

`event_time` must carry a timezone. `amount` is a decimal with ≤ 2 places. `category` must be one
of the 14 card categories. `source` is `live`, `replay` or `simulated`. The response is a
`ScoreResponse` (`assessment_id`, `status`, `risk_score`, `risk_level`, `decision`,
`decision_mode: "sandbox"`, `reasons`, model and policy versions, `processing_time_ms`,
`idempotent_replay`). Since Phase 6 it also has `policy_decision` (the model-bound policy's
decision) and `rules` (status, matched rule versions, errors, `decided_by`). `decision` is the
final action, which tenant rules can only make stricter. With no active rules, `decision` equals
`policy_decision`, as before. An idempotent replay returns the stored decision and rule trace,
even if rules changed since. Status codes: 409 for an id reused with a different payload
(`idempotency_conflict`) or matching imported, unscored history
(`transaction_exists_unscored`); 503 when scoring is unavailable under
`FRAUD_FAILURE_MODE=reject`.

## `POST /api/v1/fraud/batch`

### Request

```json
{ "items": [ <ScoreRequest>, <ScoreRequest>, ... ] }
```

* **Limit:** 1 to `FRAUD_MAX_BATCH_SIZE` items (default **500**, configurable up to 5,000).
  More items → **413** `batch_too_large`. An empty or non-list `items` → **422**. Nothing is
  scored in either case.
* Items are validated **one by one**. A malformed item does not reject the batch.

### Processing semantics

1. **Validation.** Each item is validated against `ScoreRequest`. Failures → `invalid` with field
   locations and messages (submitted values are never echoed).
2. **In-batch duplicates** (same `transaction_id`): the first occurrence in *input* order is
   processed. A later identical copy → `duplicate` (same result, `duplicate_of` = first index). A
   later copy with a different payload → `conflict` / `duplicate_id_in_batch`.
3. **Order:** remaining items are processed in **`(event_time, transaction_id)` ascending**
   order. A customer's earlier events are therefore committed before their later events are
   scored, so they are part of the later events' point-in-time history (window
   `[t − 30d, t)`). Events with the same timestamp never see each other, exactly as for single
   requests. The response lists items in **input** order with their `index`.
4. **Each item is scored by the same code path as `POST /fraud/score`** and commits in its **own
   database transaction**: transaction row, assessment and `fraud.score` audit record. Existing
   ids behave as for single requests:
   * identical payload → `scored`/`degraded` with `idempotent_replay: true`;
   * different payload → `conflict` / `idempotency_conflict`;
   * imported history → `conflict` / `transaction_exists_unscored`.
5. **Scoring failures** follow `FRAUD_FAILURE_MODE`:
   * `manual_review` → `degraded`, `risk_score: null`, decision `review`;
   * `reject` → `error` / `scoring_unavailable`, item not persisted; other items continue.
6. **Database or unexpected error:** that item → `error` (`database_error` / `internal_error`),
   processing **stops**, and every remaining item → `not_processed`, with `complete: false`.
   Items already committed stay committed; nothing is partially written. Retrying the same batch
   is safe: committed items come back as idempotent replays and the rest are scored.
7. A `fraud.batch_score` audit record stores the per-status counts.

The HTTP status is **200** whenever the envelope is valid, even if every item failed. Clients
must read `complete`, `summary` and each item's `status`.

### Item statuses

| Status | Persisted by this request | `result` (decision) | `error.code` |
|---|---|---|---|
| `scored` | yes (or replayed) | yes | — |
| `degraded` | yes | yes, always `review`, `risk_score: null` | — |
| `duplicate` | no (shares the earlier item's assessment) | copy of the earlier item's result | — |
| `invalid` | no | **none** | `validation_error` |
| `conflict` | no | **none** | `duplicate_id_in_batch`, `idempotency_conflict`, `transaction_exists_unscored` |
| `error` | no | **none** | `scoring_unavailable`, `database_error`, `internal_error` |
| `not_processed` | no | **none** | `not_processed` |

A failure never carries a decision, so a failed item can never be read as `allow`.

### Response

```json
{
  "batch_id": "6f3c…",
  "received": 4,
  "complete": true,
  "processing_order": "event_time ascending, then transaction_id",
  "summary": {"scored": 2, "degraded": 0, "duplicate": 1, "invalid": 1, "conflict": 0,
              "error": 0, "not_processed": 0},
  "items": [
    {"index": 0, "transaction_id": "txn_1", "status": "scored", "result": { "…ScoreResponse…" },
     "duplicate_of": null, "error": null},
    {"index": 1, "transaction_id": "txn_2", "status": "invalid", "result": null,
     "error": {"code": "validation_error", "message": "Item failed validation",
               "details": [{"loc": ["event_time"], "msg": "Value error, event_time must include a timezone offset", "type": "value_error"}]}},
    {"index": 2, "transaction_id": "txn_1", "status": "duplicate", "duplicate_of": 0,
     "result": { "…same assessment as index 0…" }, "error": null},
    {"index": 3, "transaction_id": "txn_3", "status": "scored", "result": { "…" },
     "duplicate_of": null, "error": null}
  ]
}
```

### Measured (2026-10-26)

20,000 consecutive held-out events (from 2020-06-21 12:14 UTC) were replayed into three fresh
organizations of a throwaway PostgreSQL database. Each organization had the same 30-day imported
history, and the same events were sent in three modes. Setup: Apple M4 Pro (14 cores), 24 GB,
macOS 26.6.1, Python 3.12.13, uvicorn **1 worker**, PostgreSQL 17.6 in Docker on the same host,
**one client sending one request at a time**, serving `card-fraud-xgb@1.0.0`.
`scripts/replay_score.py --batch-size N`.

| Mode | Requests | Events/s | Request latency p50 / p95 / p99 | Server time per item p50 / p95 |
|---|---|---|---|---|
| `POST /fraud/score` × 20,000 | 20,000 | 137.5 | 7.1 / 8.8 / 10.8 ms | 3.0 / 4.0 ms |
| `POST /fraud/batch`, 50 per request | 400 | **212.6** | 232 / 270 / 298 ms | 2.2 / 3.7 ms |
| `POST /fraud/batch`, 500 per request | 40 | 199.9 | 2.49 / 2.98 / 3.35 s | 2.2 / 4.2 ms |

* **Equivalence:** for all 20,000 events, both batch organizations stored **identical** features,
  scores, risk levels, decisions and reasons to the sequential organization (SQL comparison of
  every assessment). Decision counts were identical in all three modes (19,845 allow, 86
  challenge, 45 review, 24 decline). 200 offline/online parity checks per mode gave max relative
  feature difference 1.7e-12 and score difference 0.0.
* Batching gains ≈ 1.5× throughput by removing per-request HTTP and auth overhead. Items are
  still scored **sequentially inside a request**: request time grows linearly with batch size,
  and batches beyond ~50 bring no further throughput.
* This is a single-client, sequential measurement, not a capacity or concurrency benchmark;
  load testing is Phase 9.

## Model metadata

Read-only. All values come from the `model_versions` registry and the hash-verified manifests
stored at registration. Nothing is hardcoded. Responses use an allowlist of manifest fields, so
filesystem paths, training configuration, artifact file names, data rows and secrets are never
returned (tested). No endpoint changes registry state or promotes a model.

**Status semantics:**
* `active`: an API instance loaded and is serving the version. Set only at API startup;
  activation demotes any other active version of the same model name to `registered`.
* `registered`: hash-verified and known, but not served. This is a promotion candidate,
  recorded with `fraud_api.cli register-model --artifact … --policy …`, which never activates.
* `retired`: reserved for withdrawn versions.
* `serving` is computed per responding instance (`true` for the version that instance loaded).

`GET /api/v1/models`:

```json
{
  "serving": {"name": "card-fraud-xgb", "version": "1.0.0", "status": "active", "serving": true, "…": "…"},
  "versions": [
    {"id": "…", "name": "card-fraud-xgb", "version": "1.1.0", "status": "registered", "serving": false,
     "algorithm": "xgboost (imbalance strategy: none)", "feature_spec_version": "card-v1",
     "artifact_sha256": "2f216f103affdf6f73817407b92387f923154c694fa3a59d71c0f6fa5a635b57",
     "dataset_version": "ef8be78c6f87", "decision_threshold": 0.13384085893630981,
     "parent_model": "card-fraud-xgb@1.0.0",
     "bound_policy": {"version": "card-policy-card-fraud-xgb-1.1.0", "domain": "card_payments",
                      "mode": "sandbox", "on_scoring_failure": "review",
                      "thresholds": {"challenge_at": 0.010788, "review_at": 0.133841, "decline_at": 0.997096}},
     "registered_at": "…", "last_activated_at": null},
    {"id": "…", "version": "1.0.0", "status": "active", "serving": true,
     "artifact_sha256": "fa211a778e02a11fd652208adadb0b34cc39cff26e6849e546358cac1193ee11",
     "bound_policy": {"version": "card-policy-2026-10-26.1", "thresholds": {"challenge_at": 0.0122, "review_at": 0.212, "decline_at": 0.9985}, "…": "…"},
     "…": "…"}
  ]
}
```

These values were returned by the real registry in the measured run above, after
`register-model` recorded the 1.1.0 candidate.

* `GET /api/v1/models/active` returns the serving version's detail: the summary plus `domain`,
  `created_at`, `git_commit`, `model_format`, the 34 `feature_columns`, XGBoost
  `hyperparameters`, `parent_sha256`, `imbalance_strategy`, split row/positive counts,
  library `environment` and `metrics`. Returns 503 `model_unavailable` if this instance has no
  model loaded.
* `GET /api/v1/models/{id}`: same detail for any registered version (404 if unknown).
* `GET /api/v1/models/{id}/metrics`: `validation` (AP, ROC-AUC, Brier, precision/recall@K, …),
  `test_summary` (AP, ROC-AUC, precision, recall at the validation threshold, computed once
  after selection), `validation_operating_points` and `decision_threshold_method`. These are
  training-time metrics from the manifest, not live monitoring.

## Known limitations

* Batch items are scored sequentially in one request on one worker; a 500-item batch holds a
  worker for about 2.5 s. Use batches of about 50 for latency-sensitive callers.
* The batch limit is an item count. There is no request-body byte limit or rate limiting yet
  (Phase 10), so very large bodies are parsed before the 413 check.
* A database error stops the batch rather than retrying internally; the caller retries
  (idempotently).
* Cross-batch ordering is the caller's responsibility. An event that arrives later than newer
  events of the same customer sees only the history already stored, exactly like single requests.
* The model registry is platform-wide (not per tenant) and contains no tenant data.
  `serving` reflects the answering instance only; during a rolling deploy, instances may serve
  different versions.
* `bound_policy` for the active version is recorded at API startup from the loaded policy. A
  registry migrated from Phase 3 shows `null` until the API restarts.
