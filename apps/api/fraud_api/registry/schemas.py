from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BoundThresholds(BaseModel):
    challenge_at: float
    review_at: float
    decline_at: float


class BoundPolicy(BaseModel):
    version: str
    domain: str
    mode: Literal["sandbox"]
    thresholds: BoundThresholds
    on_scoring_failure: str


class ModelVersionSummary(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    status: Literal["active", "registered", "retired"] = Field(
        description="Registry status. 'registered' = known, hash-verified, not served (candidate)."
    )
    serving: bool = Field(description="True if the API instance answering loaded this version")
    algorithm: str | None
    feature_spec_version: str | None
    artifact_sha256: str
    dataset_version: str | None
    decision_threshold: float | None
    parent_model: str | None
    bound_policy: BoundPolicy | None = Field(
        description="Decision policy bound to this version, if one was registered or activated"
    )
    registered_at: datetime
    last_activated_at: datetime | None


class ModelMetrics(BaseModel):
    validation: dict[str, float]
    test_summary: dict[str, float]
    validation_operating_points: list[dict[str, float]]
    decision_threshold_method: str | None


class ModelVersionDetail(ModelVersionSummary):
    domain: str | None
    created_at: str | None
    git_commit: str | None
    model_format: str | None
    feature_columns: list[str]
    hyperparameters: dict[str, float | int | str]
    parent_sha256: str | None
    imbalance_strategy: str | None
    splits: dict[str, dict[str, int]]
    environment: dict[str, str]
    metrics: ModelMetrics


class ModelsResponse(BaseModel):
    serving: ModelVersionSummary | None = Field(
        description="Model loaded by the answering API instance (null if none loaded)"
    )
    versions: list[ModelVersionSummary]
