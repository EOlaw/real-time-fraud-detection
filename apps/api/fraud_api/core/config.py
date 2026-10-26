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

    @field_validator("jwt_secret")
    @classmethod
    def _strong_secret(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32 or v.get_secret_value() == "change-me":
            raise ValueError("FRAUD_JWT_SECRET must be at least 32 random characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
