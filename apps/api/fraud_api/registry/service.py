"""Model registry: registration, activation and read-only metadata.

Status semantics (``model_versions.status``):
  ``active``      the version an API instance loaded and is serving. Set only at API startup, from
                  the hash-verified artifact actually loaded. Activating a version demotes any other
                  active version of the same model name to ``registered``.
  ``registered``  known and hash-verified, not served. A promotion candidate is registered with its
                  bound policy (``register-model``); registration never activates.
  ``retired``     reserved for withdrawn versions.

``serving`` in API responses is computed per instance (the model this process loaded), so a
registry row and the answering instance can be compared directly.

Metadata responses are built from an allowlist of manifest fields. Training configuration (which
contains filesystem paths), artifact file names and any data rows are never returned.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from fraud_api.db.models import ModelVersion
from fraud_api.registry.schemas import (
    BoundPolicy,
    ModelMetrics,
    ModelVersionDetail,
    ModelVersionSummary,
)
from fraud_api.scoring.policy import DecisionPolicy
from fraud_api.scoring.schemas import utcnow
from fraud_ml.models.artifact import ArtifactError, LoadedModel


def policy_summary(policy: DecisionPolicy) -> dict[str, Any]:
    return {
        "version": policy.version,
        "domain": policy.domain,
        "mode": policy.mode,
        "thresholds": policy.thresholds.model_dump(),
        "on_scoring_failure": policy.on_scoring_failure,
    }


def register_model(
    session: Session,
    model: LoadedModel,
    policy: DecisionPolicy | None = None,
    activate: bool = False,
) -> uuid.UUID:
    """Record a hash-verified model version (idempotent).

    ``activate=True`` is used only by API startup for the model it loaded. Re-using a name@version
    for different bytes is refused. ``INSERT ... ON CONFLICT DO NOTHING`` keeps concurrent workers
    starting against a fresh database from racing on the unique (name, version) constraint.
    """
    if policy is not None:
        policy.check_model(model.name, model.version)
    session.execute(
        pg_insert(ModelVersion)
        .values(
            id=uuid.uuid4(),
            name=model.name,
            version=model.version,
            artifact_sha256=model.sha256,
            dataset_version=model.manifest.get("dataset_version"),
            manifest=model.manifest,
            status="registered",
        )
        .on_conflict_do_nothing(constraint="uq_model_versions_name_version")
    )
    existing = session.scalars(
        select(ModelVersion).where(
            ModelVersion.name == model.name, ModelVersion.version == model.version
        )
    ).one()
    if existing.artifact_sha256 != model.sha256:
        session.rollback()
        raise ArtifactError(
            f"{model.name}@{model.version} is registered with a different "
            "artifact hash; bump the model version instead of overwriting"
        )
    if policy is not None:
        existing.bound_policy = policy_summary(policy)
    if activate:
        session.execute(
            update(ModelVersion)
            .where(
                ModelVersion.name == model.name,
                ModelVersion.status == "active",
                ModelVersion.id != existing.id,
            )
            .values(status="registered")
        )
        existing.status = "active"
        existing.last_activated_at = utcnow()
    session.commit()
    return existing.id


def _summary(mv: ModelVersion, serving_id: uuid.UUID | None) -> dict[str, Any]:
    m = mv.manifest
    lineage = m.get("lineage") or {}
    threshold = (m.get("decision_threshold") or {}).get("value")
    return {
        "id": mv.id,
        "name": mv.name,
        "version": mv.version,
        "status": mv.status,
        "serving": serving_id is not None and mv.id == serving_id,
        "algorithm": m.get("algorithm"),
        "feature_spec_version": m.get("feature_spec_version"),
        "artifact_sha256": mv.artifact_sha256,
        "dataset_version": mv.dataset_version,
        "decision_threshold": threshold,
        "parent_model": lineage.get("parent_model"),
        "bound_policy": BoundPolicy.model_validate(mv.bound_policy) if mv.bound_policy else None,
        "registered_at": mv.created_at,
        "last_activated_at": mv.last_activated_at,
    }


def list_versions(session: Session, serving_id: uuid.UUID | None) -> list[ModelVersionSummary]:
    rows = session.scalars(
        select(ModelVersion).order_by(ModelVersion.name, ModelVersion.created_at)
    ).all()
    return [ModelVersionSummary(**_summary(mv, serving_id)) for mv in rows]


def _metrics(m: dict[str, Any]) -> ModelMetrics:
    def floats(d: Any) -> dict[str, float]:
        return {k: float(v) for k, v in (d or {}).items() if isinstance(v, (int, float))}

    return ModelMetrics(
        validation=floats(m.get("validation_metrics")),
        test_summary=floats(m.get("test_metrics_summary")),
        validation_operating_points=[floats(op) for op in m.get("validation_operating_points", [])],
        decision_threshold_method=(m.get("decision_threshold") or {}).get("method"),
    )


def get_version(
    session: Session, model_version_id: uuid.UUID, serving_id: uuid.UUID | None
) -> ModelVersionDetail | None:
    mv = session.get(ModelVersion, model_version_id)
    if mv is None:
        return None
    m = mv.manifest
    lineage = m.get("lineage") or {}
    xgb_params = ((m.get("training_config") or {}).get("models") or {}).get("xgboost") or {}
    splits = {
        name: {k: v for k, v in (s or {}).items() if k in ("rows", "positives")}
        for name, s in (m.get("splits") or {}).items()
    }
    return ModelVersionDetail(
        **_summary(mv, serving_id),
        domain=m.get("domain"),
        created_at=m.get("created_at"),
        git_commit=m.get("git_commit"),
        model_format=m.get("model_format"),
        feature_columns=list(m.get("feature_columns") or []),
        hyperparameters={k: v for k, v in xgb_params.items() if isinstance(v, (int, float, str))},
        parent_sha256=lineage.get("parent_sha256"),
        imbalance_strategy=lineage.get("imbalance_strategy"),
        splits=splits,
        environment={k: str(v) for k, v in (m.get("environment") or {}).items()},
        metrics=_metrics(m),
    )


def get_metrics(session: Session, model_version_id: uuid.UUID) -> ModelMetrics | None:
    mv = session.get(ModelVersion, model_version_id)
    return _metrics(mv.manifest) if mv is not None else None
