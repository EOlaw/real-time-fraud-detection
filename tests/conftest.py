from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from fraud_ml.training.train import TrainConfig, run_training
from tests.fixtures import synthetic_events

TEST_MODEL_NAME = "test-model"
TEST_MODEL_VERSION = "0.0.1"


def small_train_config(**overrides: Any) -> TrainConfig:
    cfg: dict[str, Any] = {
        "model_name": TEST_MODEL_NAME,
        "model_version": TEST_MODEL_VERSION,
        "seed": 1,
        "splits": {
            "warmup_end": "2020-01-08T00:00:00Z",
            "train_end": "2020-02-20T00:00:00Z",
            "validation_end": "2020-03-10T00:00:00Z",
        },
        "evaluation": {
            "review_capacity_fraction": 0.02,
            "alert_rates": [0.01, 0.05],
            "threshold_fbeta": 2.0,
            "bootstrap_resamples": 20,
            "selection_ap_tolerance": 0.005,
        },
        "models": {
            "logistic_regression": {"C": 1.0, "max_iter": 500},
            "random_forest": {"n_estimators": 20, "min_samples_leaf": 5, "max_features": "sqrt"},
            "xgboost": {
                "n_estimators": 40,
                "learning_rate": 0.2,
                "max_depth": 3,
                "subsample": 1.0,
                "colsample_bytree": 1.0,
                "min_child_weight": 1.0,
                "reg_lambda": 1.0,
            },
        },
    }
    cfg.update(overrides)
    return TrainConfig.model_validate(cfg)


@pytest.fixture(scope="session")
def trained_artifact(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small model trained on the synthetic fixture (not the real dataset)."""
    out = tmp_path_factory.mktemp("artifact") / "model"
    events = synthetic_events(n=6_000, n_customers=40, seed=11)
    run_training(
        small_train_config(),
        events,
        out,
        {"dataset_version": "synthetic-fixture"},
        log=lambda *_: None,
    )
    return out


@pytest.fixture(scope="session")
def policy_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("policy") / "policy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": "test-policy-1",
                "domain": "card_payments",
                "mode": "sandbox",
                "model": {"name": TEST_MODEL_NAME, "version": TEST_MODEL_VERSION},
                "thresholds": {"challenge_at": 0.1, "review_at": 0.5, "decline_at": 0.95},
                "on_scoring_failure": "review",
            }
        )
    )
    return path
