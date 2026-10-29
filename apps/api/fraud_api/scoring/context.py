"""Construction of the scoring context shared by the API and the stream workers.

Both processes load the hash-verified artifact and its bound policy the same way and record the
loaded version in the registry, so a stream worker can never score with a different model or
policy than an API instance started from the same configuration.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from fraud_api.core.config import Settings
from fraud_api.core.logging import get_logger
from fraud_api.registry.service import register_model
from fraud_api.scoring.policy import DecisionPolicy
from fraud_api.scoring.service import ScoringContext
from fraud_ml.models.artifact import ArtifactError, LoadedModel, load_artifact

log = get_logger(__name__)


def build_scoring_context(
    settings: Settings, session_factory: sessionmaker[Session]
) -> ScoringContext:
    policy = DecisionPolicy.load(settings.policy_path)  # invalid policy: fail fast
    model: LoadedModel | None
    model_version_id: uuid.UUID | None = None
    try:
        model = load_artifact(settings.model_dir)
    except (ArtifactError, OSError) as exc:
        # Start, but report not-ready; scoring follows FRAUD_FAILURE_MODE.
        log.error("model_load_failed", error=str(exc), model_dir=str(settings.model_dir))
        model = None
    if model is not None:
        policy.check_model(model.name, model.version)  # mismatch: fail fast
        with session_factory() as session:
            # Activation records the loaded model and its bound policy in the registry and
            # demotes any previously active version of the same name to 'registered'.
            model_version_id = register_model(session, model, policy, activate=True)
        log.info(
            "model_loaded",
            model=model.name,
            version=model.version,
            sha256=model.sha256[:12],
            policy=policy.version,
        )
    return ScoringContext(
        model=model,
        model_version_id=model_version_id,
        policy=policy,
        failure_mode=settings.failure_mode,
        max_history_rows=settings.max_history_rows,
    )
