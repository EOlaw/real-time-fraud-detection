"""Prometheus metrics with bounded labels.

Cardinality rules (tested in tests/unit/test_monitoring_unit.py and
tests/integration/test_observability.py):

* Every label value comes from a closed set defined here, or from the matched **route template**
  (``/api/v1/cases/{case_id}``, never the concrete path). Unmatched paths are ``"unmatched"``.
* Never used as labels: transaction, customer, merchant, case, assessment or user ids, tenant
  ids, tokens, raw URLs, query strings, rule keys (tenant-defined), or any request input.
* ``normalize`` maps anything outside an allowlist to ``"other"`` so a bug cannot create an
  unbounded series.

Each process has its own registry (``REGISTRY``). The API exposes it on ``GET /metrics`` (bearer
token, see ``fraud_api.observability.endpoint``); stream workers expose theirs with
``--metrics-port``. Multi-process API servers (``uvicorn --workers N``) are not aggregated here;
run one API process per metrics endpoint or use the Prometheus multiprocess mode (not set up).
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY: Final = CollectorRegistry(auto_describe=True)

METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
SCORING_PATHS: Final = frozenset({"single", "batch", "stream"})
OUTCOMES: Final = frozenset({"scored", "degraded", "replayed"})
DECISIONS: Final = frozenset({"allow", "challenge", "review", "decline"})
STAGES: Final = frozenset({"features", "inference", "rules", "total"})
BATCH_STATUSES: Final = frozenset(
    {"scored", "degraded", "duplicate", "invalid", "conflict", "error", "not_processed"}
)
COMPONENTS: Final = frozenset({"api", "worker", "monitoring"})
REDIS_OPS: Final = frozenset({"ingest", "feed_publish", "sse", "status", "worker"})
WORKER_OUTCOMES: Final = frozenset(
    {"scored", "degraded", "replayed", "retried", "dead_lettered", "late_arrival", "lease_lost"}
)
INGEST_RESULTS: Final = frozenset({"accepted", "rejected", "backpressure"})

# Latency buckets in seconds: sub-millisecond model stages up to multi-second batches.
_FAST = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
_HTTP = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


# Which entry point is scoring (set by batch and the stream worker; default single request).
SCORING_PATH: ContextVar[str] = ContextVar("scoring_path", default="single")


def normalize(value: str | None, allowed: Iterable[str]) -> str:
    return value if value is not None and value in allowed else "other"


HTTP_REQUESTS = Counter(
    "fraud_http_requests",
    "HTTP requests by method, route template and status code",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "fraud_http_request_duration_seconds",
    "HTTP request duration (server side, excluding streaming bodies)",
    ["method", "route"],
    buckets=_HTTP,
    registry=REGISTRY,
)
SCORING_DURATION = Histogram(
    "fraud_scoring_duration_seconds",
    "Scoring stage duration as recorded in timings_ms (validation to decision)",
    ["path", "stage"],
    buckets=_FAST,
    registry=REGISTRY,
)
SCORING_DECISIONS = Counter(
    "fraud_scoring_decisions",
    "Persisted scoring outcomes by path, outcome and final decision",
    ["path", "outcome", "decision"],
    registry=REGISTRY,
)
RULE_ESCALATIONS = Counter(
    "fraud_rule_escalations",
    "Assessments whose final decision is stricter than the policy decision",
    ["path"],
    registry=REGISTRY,
)
BATCH_ITEMS = Counter("fraud_batch_items", "Batch item outcomes", ["status"], registry=REGISTRY)
BATCH_REQUESTS = Counter(
    "fraud_batch_requests", "Batch requests by completeness", ["complete"], registry=REGISTRY
)
DB_ERRORS = Counter(
    "fraud_db_errors",
    "Database errors (SQLAlchemy) by component",
    ["component"],
    registry=REGISTRY,
)
REDIS_ERRORS = Counter(
    "fraud_redis_errors",
    "Redis errors by component and operation",
    ["component", "operation"],
    registry=REGISTRY,
)
INGEST_EVENTS = Counter(
    "fraud_ingest_events",
    "Events submitted to POST /events by result",
    ["result"],
    registry=REGISTRY,
)
WORKER_MESSAGES = Counter(
    "fraud_stream_worker_messages",
    "Stream worker message outcomes (includes retries and dead letters)",
    ["outcome"],
    registry=REGISTRY,
)
SSE_CONNECTIONS = Gauge(
    "fraud_sse_connections", "Open SSE feed connections (this process)", registry=REGISTRY
)
SSE_CONNECTIONS_TOTAL = Counter(
    "fraud_sse_connections_opened", "SSE feed connections opened", registry=REGISTRY
)
SSE_EVENTS = Counter(
    "fraud_sse_events_sent", "SSE events sent by type", ["event"], registry=REGISTRY
)
MODEL_INFO = Gauge(
    "fraud_model_info",
    "Model and policy loaded by this process (value 1)",
    ["model", "version", "policy"],
    registry=REGISTRY,
)


def observe_scoring(
    status: str, decision: str, replay: bool, escalated: bool, timings_ms: dict[str, float]
) -> None:
    p = normalize(SCORING_PATH.get(), SCORING_PATHS)
    outcome = "replayed" if replay else normalize(status, OUTCOMES)
    SCORING_DECISIONS.labels(p, outcome, normalize(decision, DECISIONS)).inc()
    if replay:
        return  # nothing was computed
    if escalated:
        RULE_ESCALATIONS.labels(p).inc()
    for stage, ms in timings_ms.items():
        if stage in STAGES and ms == ms:  # skip NaN
            SCORING_DURATION.labels(p, stage).observe(ms / 1000.0)
