# Operations: metrics, Prometheus and Grafana (Phase 9)

This covers **operational** monitoring (is the service healthy and fast?). **Model** monitoring
(drift, label-matured performance, promotion gates) is in
[docs/ml/monitoring.md](../ml/monitoring.md).

## Implemented vs designed

| | Status |
|---|---|
| Prometheus metrics in the API (`GET /metrics`) and in each stream worker (`--metrics-port`) | Implemented and tested |
| Local Prometheus + Grafana (Compose profile `monitoring`), 8 alert rules, a 16-panel dashboard | Implemented; validated locally (see below) |
| Alert delivery (Alertmanager, paging, chat) | **Not implemented.** Rules evaluate in Prometheus only |
| Managed Prometheus/Grafana, a hosted scheduler, AWS | **Not available** in this environment (Phase 10) |
| Multi-process metrics (`uvicorn --workers N`) | **Not implemented**; see [Limitations](#limitations) |

## Metrics

All metrics are in one registry (`fraud_api.observability.metrics`). **Labels are bounded by
construction:**
- **Routes** are FastAPI route **templates** (`/api/v1/cases/{case_id}`), never raw URLs. Any
  request without a matched route (404 scans, for example) is labelled `unmatched`.
- **Every label value** goes through `normalize()` against a fixed allowlist; anything else becomes
  `other`.
- **Never used as labels:** transaction, customer, merchant, case or assessment ids, tenant ids,
  tokens, emails, query strings, or any free text.
- **Tests:** `tests/unit/test_monitoring_unit.py` checks route-template cardinality and the
  allowlist. `tests/integration/test_observability.py` scrapes after real traffic and asserts that
  no identifier from that traffic appears in the output.

| Metric | Type | Labels |
|---|---|---|
| `fraud_http_requests_total` | counter | `method`, `route`, `status` |
| `fraud_http_request_duration_seconds` | histogram | `method`, `route` |
| `fraud_scoring_duration_seconds` | histogram | `path` (single/batch/stream), `stage` (features/inference/rules/total) |
| `fraud_scoring_decisions_total` | counter | `path`, `outcome` (scored/degraded/replayed), `decision` |
| `fraud_rule_escalations_total` | counter | `path` |
| `fraud_batch_items_total`, `fraud_batch_requests_total` | counter | `status`; `complete` |
| `fraud_db_errors_total`, `fraud_redis_errors_total` | counter | `component` (api/worker/monitoring); plus `operation` (ingest/feed_publish/sse/status/worker) |
| `fraud_ingest_events_total` | counter | `result` (accepted/rejected/backpressure) |
| `fraud_stream_worker_messages_total` | counter | `outcome` (scored/degraded/replayed/retried/dead_lettered/late_arrival/lease_lost) |
| `fraud_sse_connections` (gauge), `fraud_sse_connections_opened_total`, `fraud_sse_events_sent_total` | | `event` |
| `fraud_model_info` | gauge = 1 | `model`, `version`, `policy` (one series per serving process) |
| `fraud_stream_up`, `fraud_stream_backlog`, `fraud_stream_pending`, `fraud_stream_consumer_lag_seconds`, `fraud_stream_dead_letters` | gauges, read from Redis **at scrape time** | `shard` (0–7) |

**Database overload.** A connection-pool timeout or operational database error returns `503
database_unavailable` with `Retry-After: 2`, not a 500, and counts `fraud_db_errors_total`. Other
database errors stay 500.

## Access control for `/metrics`

- **Token required:** `/metrics` needs `Authorization: Bearer <FRAUD_METRICS_TOKEN>` (at least 24
  characters, compared in constant time).
- **No token configured:** the endpoint returns **404**, so metrics are off by default.
- **Separate from user auth:** the token is not a user JWT and grants nothing else.
- **Separate from health checks:** `/metrics` is not in the OpenAPI schema. `/health/live` and
  `/health/ready` stay public and unauthenticated for orchestrators; they expose no metrics, and
  metrics expose no health detail beyond `fraud_stream_up`.
- **Worker metrics:** workers serve metrics without a token, **bound to 127.0.0.1 only**
  (`--metrics-port`). In a container deployment they must stay on a private network.

## Running it locally

```bash
# in .env: FRAUD_METRICS_TOKEN=$(openssl rand -hex 32), GRAFANA_ADMIN_PASSWORD=<random>
make monitoring-up       # writes infrastructure/monitoring/secrets/metrics_token (mode 600,
                         # git-ignored), starts Prometheus 127.0.0.1:9090, Grafana 127.0.0.1:3001
make api                 # the API on :8000, the Prometheus target
uv run python -m fraud_api.stream.worker --worker-id w1 --shards 0-3 --metrics-port 9101
uv run python -m fraud_api.stream.worker --worker-id w2 --shards 4-7 --metrics-port 9102
```

- **Grafana:** log in as `admin` with `GRAFANA_ADMIN_PASSWORD`. The dashboard **Fraud platform:
  operations** (uid `fraud-operations`) and the Prometheus datasource are provisioned from
  `infrastructure/monitoring/grafana/`.
- **Scraping:** Prometheus reaches host processes through `host.docker.internal`. On Docker Desktop
  (macOS) that also reaches services bound to 127.0.0.1. On Linux, bind the API and workers to the
  bridge address or run them in Compose.

**Local validation (2026-10-28, against a throwaway database and Redis DB):**
- `promtool check config` reported the config valid with 8 rules.
- All three targets were `up`, with the API scraped using the token file.
- Dashboard queries returned data during the load tests, and the rules evaluated (inactive).
- Grafana provisioned the 16-panel dashboard, and the datasource health check passed.

## Alert rules (`infrastructure/monitoring/alerts.yml`)

| Alert | Condition | Severity |
|---|---|---|
| FraudApiDown | API target down 1 min | page |
| FraudApi5xxRate | > 2% 5xx over 5 min, for 5 min | page |
| FraudScoringLatencyHigh | server-side scoring p95 > 250 ms for 10 min | ticket |
| FraudStreamLag | oldest stream message > 60 s for 5 min | page |
| FraudDeadLetters | any dead letter, 1 min | ticket |
| FraudRedisUnavailable | `fraud_stream_up == 0`, 1 min | page |
| FraudDatabaseErrors | any database error in 5 min | ticket |
| FraudDegradedScores | > 1% degraded scores over 10 min | page |

**How thresholds were chosen.** These are **starting points, not SLOs.** There is no production
traffic to derive them from.
- **Latency:** 250 ms is roughly 20× the measured steady-state p95 (11.8 ms at 150/s), so it fires
  on saturation, not jitter.
- **Stream lag:** 60 s is well below the burst drain time measured for 20,000 queued events (92 s).
  It fires during such a burst, which is intended: a backlog that size is worth a look.
- **Severity labels are inert:** nothing routes them yet (no Alertmanager).

## Limitations

- **Multi-process metrics:** with `uvicorn --workers 2` (the Docker image), each scrape reaches
  one process, so counters are per-process and undercount. Prometheus multiprocess mode
  (`PROMETHEUS_MULTIPROC_DIR`) is not configured. All measurements here used one API process.
- **Stream gauges** are read from Redis at scrape time; a slow Redis slows the scrape (bounded by
  the client timeout).
- **Graceful shutdown:** the image uses `--timeout-graceful-shutdown 10`. Open SSE feeds are cut
  after 10 s on restart, and clients resume with `Last-Event-ID`.
- **Container default:** the image sets `OMP_NUM_THREADS=1`; the measured effect is in
  [load-testing.md](load-testing.md#xgboost-threads).
