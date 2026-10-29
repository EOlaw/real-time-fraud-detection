from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FailureMode(StrEnum):
    """What the scoring endpoint does when features or inference fail.

    * ``manual_review``: persist the event with ``status=degraded``, no score, decision ``review``.
      Fraud is not silently allowed, but every failure adds analyst workload.
    * ``reject``: return HTTP 503 and persist nothing; the caller applies its own fallback.
    A failure is never converted into a low-risk ``allow`` decision.
    """

    MANUAL_REVIEW = "manual_review"
    REJECT = "reject"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FRAUD_", env_file=".env", extra="ignore")

    database_url: SecretStr
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)
    jwt_issuer: str = "fraud-intel"
    jwt_audience: str = "fraud-api"
    model_dir: Path = Path("ml/artifacts/current")
    policy_path: Path = Path("ml/configs/decision_policy.yaml")
    failure_mode: FailureMode = FailureMode.MANUAL_REVIEW
    log_level: str = "INFO"
    # Upper bound on history rows read per scoring request; exceeding it is an error, not a
    # silent truncation (truncated history would understate velocity features).
    max_history_rows: int = Field(default=20_000, ge=100)
    db_pool_size: int = Field(default=10, ge=1)
    # Items per POST /api/v1/fraud/batch request (413 above). Items are scored sequentially inside
    # the request, so the limit bounds request duration as well as payload size.
    max_batch_size: int = Field(default=500, ge=1, le=5_000)

    # ---- Redis Streams (Phase 5). Optional: without a URL the API works as before and the
    # streaming endpoints return 503 stream_unavailable.
    redis_url: SecretStr | None = None
    # Key prefix for every stream/hash/lease key (lets tests and environments share a Redis).
    stream_prefix: str = Field(default="fraud", pattern=r"^[A-Za-z0-9_.:\-]+$")
    # Customer-hash shards. Changing this while messages are queued breaks per-customer ordering:
    # drain the streams first.
    stream_shards: int = Field(default=8, ge=1, le=256)
    # Backpressure: ingest returns 503 if a target shard already holds this many unprocessed
    # messages (processed messages are deleted from the shard stream).
    stream_max_backlog: int = Field(default=100_000, ge=1)
    # Events older than (latest stored event of the same customer - tolerance) are not scored;
    # they go to the dead-letter stream for investigation (late_beyond_tolerance).
    stream_late_tolerance_seconds: int = Field(default=7 * 24 * 3600, ge=0)
    # Assessment feed (SSE source): approximate entries retained per tenant.
    feed_max_len: int = Field(default=100_000, ge=100)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    # Connections are closed after this; clients reconnect with Last-Event-ID (and a fresh token).
    sse_max_connection_seconds: float = Field(default=1800.0, gt=0)

    # ---- Labels (Phase 8). Default maturity for label-matured performance: transactions newer
    # than (as_of - maturity) are excluded because their outcome may not be known yet.
    label_maturity_days: int = Field(default=30, ge=0, le=365)

    # ---- Observability (Phase 9). GET /metrics is disabled (404) unless a token is set; Prometheus
    # must then send it as a bearer token. Use a long random value, distinct from the JWT secret.
    metrics_token: SecretStr | None = None

    @field_validator("metrics_token")
    @classmethod
    def _strong_metrics_token(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 24:
            raise ValueError("FRAUD_METRICS_TOKEN must be at least 24 random characters")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32 or v.get_secret_value() == "change-me":
            raise ValueError("FRAUD_JWT_SECRET must be at least 32 random characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
