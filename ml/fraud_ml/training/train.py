"""Train, compare and package card-fraud models with chronological validation.

Usage:
    uv run python -m fraud_ml.training.train --config ml/configs/train_card_v1.yaml

Protocol (see docs/ml/model-development-plan.md):
  1. Point-in-time features are computed over the full event stream (features only look back).
  2. Chronological split: warm-up | train | validation | test (held out).
  3. Every candidate is fit on train and compared on validation by average precision.
  4. The decision threshold and alert-budget operating points are chosen on validation.
  5. The test window is scored once, after selection, for reporting.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import sklearn
import xgboost as xgb
import yaml
from pydantic import BaseModel, ConfigDict
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from fraud_ml.domains.card_payments import DOMAIN
from fraud_ml.evaluation.metrics import (
    best_fbeta_threshold,
    calibration_table,
    cluster_bootstrap_ci,
    operating_points,
    ranking_metrics,
    threshold_metrics,
    top_k_metrics,
)
from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import MODEL_COLUMNS, NUMERIC_FEATURES, encode
from fraud_ml.models.artifact import save_artifact


class SplitConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    warmup_end: datetime
    train_end: datetime
    validation_end: datetime


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    review_capacity_fraction: float
    alert_rates: list[float]
    threshold_fbeta: float
    bootstrap_resamples: int
    # Candidates within this validation-AP margin of the best are treated as tied; ties go to the
    # better-calibrated model (lower Brier), since risk bands are defined on probabilities.
    selection_ap_tolerance: float


class TrainConfig(BaseModel):
    model_config = ConfigDict(frozen=True, protected_namespaces=())
    model_name: str
    model_version: str
    seed: int
    splits: SplitConfig
    evaluation: EvaluationConfig
    models: dict[str, dict[str, Any]]


# Candidates and whether the serving path can load them (XGBoost JSON only; see artifact.py).
SERVABLE = {"xgboost", "xgboost_weighted"}


def build_matrix(features: pd.DataFrame) -> npt.NDArray[np.float32]:
    return encode(
        {n: features[n].to_numpy() for n in NUMERIC_FEATURES},
        {"category": features["category"].to_numpy(), "channel": features["channel"].to_numpy()},
    )


def candidates(cfg: TrainConfig, y_train: npt.NDArray[np.int64]) -> dict[str, Any]:
    neg_pos = float((y_train == 0).sum() / max((y_train == 1).sum(), 1))
    xgb_params = dict(cfg.models["xgboost"])
    common_xgb = dict(
        tree_method="hist",
        objective="binary:logistic",
        eval_metric="aucpr",
        random_state=cfg.seed,
        n_jobs=-1,
    )
    lr = cfg.models["logistic_regression"]
    rf = cfg.models["random_forest"]
    return {
        "dummy_prior": DummyClassifier(strategy="prior"),
        "logistic_regression": make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            StandardScaler(),
            LogisticRegression(C=lr["C"], max_iter=lr["max_iter"], class_weight="balanced"),
        ),
        # sklearn >= 1.4 forests handle NaN natively; no imputation needed.
        "random_forest": RandomForestClassifier(
            n_estimators=rf["n_estimators"],
            min_samples_leaf=rf["min_samples_leaf"],
            max_features=rf["max_features"],
            class_weight="balanced_subsample",
            random_state=cfg.seed,
            n_jobs=-1,
        ),
        "xgboost": xgb.XGBClassifier(**xgb_params, **common_xgb),
        # Same model with inverse-prevalence class weighting, to test (not assume) that
        # reweighting helps.
        "xgboost_weighted": xgb.XGBClassifier(**xgb_params, **common_xgb, scale_pos_weight=neg_pos),
    }


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed argv, no user input
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def select_model(validation: dict[str, Any], ap_tolerance: float) -> str:
    """Best servable validation AP; near-ties (within tolerance) go to the lowest Brier score."""
    best_ap = max(validation[n]["average_precision"] for n in SERVABLE)
    tied = [n for n in SERVABLE if validation[n]["average_precision"] >= best_ap - ap_tolerance]
    return min(sorted(tied), key=lambda n: validation[n]["brier"])


def split_masks(events: pd.DataFrame, s: SplitConfig) -> dict[str, npt.NDArray[np.bool_]]:
    t = events["event_time"]
    masks = {
        "train": ((t >= s.warmup_end) & (t < s.train_end)).to_numpy(),
        "validation": ((t >= s.train_end) & (t < s.validation_end)).to_numpy(),
        "test": (t >= s.validation_end).to_numpy(),
    }
    assert not (masks["train"] & masks["validation"]).any()
    assert not (masks["validation"] & masks["test"]).any()
    return masks


def run_training(
    cfg: TrainConfig,
    events: pd.DataFrame,
    out_dir: Path,
    dataset_manifest: dict[str, Any],
    log: Any = print,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    events = events.sort_values(["event_time", "event_id"], kind="mergesort").reset_index(drop=True)
    features = compute_offline_features(events)
    x = build_matrix(features)
    y = events["is_fraud"].to_numpy(np.int64)
    masks = split_masks(events, cfg.splits)
    log(f"features: {x.shape} in {time.perf_counter() - t0:.1f}s")

    split_summary = {
        name: {
            "rows": int(m.sum()),
            "positives": int(y[m].sum()),
            "prevalence": float(y[m].mean()) if m.any() else float("nan"),
            "time_range": [
                events.loc[m, "event_time"].min().isoformat(),
                events.loc[m, "event_time"].max().isoformat(),
            ]
            if m.any()
            else None,
        }
        for name, m in masks.items()
    }
    xtr, ytr = x[masks["train"]], y[masks["train"]]
    xva, yva = x[masks["validation"]], y[masks["validation"]]
    xte, yte = x[masks["test"]], y[masks["test"]]

    fitted: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    k_val = round(cfg.evaluation.review_capacity_fraction * len(yva))
    for name, model in candidates(cfg, ytr).items():
        start = time.perf_counter()
        model.fit(xtr, ytr)
        p = model.predict_proba(xva)[:, 1]
        fitted[name] = model
        validation[name] = {
            **ranking_metrics(yva, p),
            **top_k_metrics(yva, p, k_val),
            "brier": float(brier_score_loss(yva, p)),
            "fit_seconds": time.perf_counter() - start,
            "servable": name in SERVABLE,
        }
        log(
            f"{name}: val AP={validation[name]['average_precision']:.4f} "
            f"({validation[name]['fit_seconds']:.1f}s)"
        )

    best_overall = max(validation, key=lambda n: validation[n]["average_precision"])
    selected = select_model(validation, cfg.evaluation.selection_ap_tolerance)
    model = fitted[selected]
    p_val = model.predict_proba(xva)[:, 1]
    threshold = best_fbeta_threshold(yva, p_val, beta=cfg.evaluation.threshold_fbeta)
    val_ops = operating_points(yva, p_val, cfg.evaluation.alert_rates)

    # ---- Held-out test: scored once, after every selection decision above. ----
    p_test = model.predict_proba(xte)[:, 1]
    groups = events.loc[masks["test"], "customer_id"].to_numpy()
    seed, n_boot = cfg.seed, cfg.evaluation.bootstrap_resamples
    k_test = round(cfg.evaluation.review_capacity_fraction * len(yte))
    test_channels = features.loc[masks["test"], "channel"].to_numpy()
    segments = {
        ch: {
            **ranking_metrics(yte[test_channels == ch], p_test[test_channels == ch]),
            **threshold_metrics(yte[test_channels == ch], p_test[test_channels == ch], threshold),
        }
        for ch in np.unique(test_channels)
        if yte[test_channels == ch].sum() > 0
    }
    test_report: dict[str, Any] = {
        **ranking_metrics(yte, p_test),
        "at_validation_threshold": threshold_metrics(yte, p_test, threshold),
        "top_k": top_k_metrics(yte, p_test, k_test),
        "average_precision_ci": cluster_bootstrap_ci(
            yte,
            p_test,
            groups,
            lambda a, b: float(average_precision_score(a, b)),
            n_resamples=n_boot,
            seed=seed,
        ),
        "recall_at_threshold_ci": cluster_bootstrap_ci(
            yte,
            p_test,
            groups,
            lambda a, b: threshold_metrics(a, b, threshold)["recall"],
            n_resamples=n_boot,
            seed=seed,
        ),
        "precision_at_threshold_ci": cluster_bootstrap_ci(
            yte,
            p_test,
            groups,
            lambda a, b: threshold_metrics(a, b, threshold)["precision"],
            n_resamples=n_boot,
            seed=seed,
        ),
        "calibration": calibration_table(yte, p_test),
        "segments_by_channel": segments,
    }
    posthoc = {name: ranking_metrics(yte, m.predict_proba(xte)[:, 1]) for name, m in fitted.items()}

    booster = model.get_booster()
    booster.feature_names = list(MODEL_COLUMNS)
    manifest = save_artifact(
        booster,
        out_dir,
        {
            "model_name": cfg.model_name,
            "model_version": cfg.model_version,
            "domain": DOMAIN,
            "algorithm": selected,
            "created_at": datetime.now(UTC).isoformat(),
            "git_commit": _git_commit(),
            "dataset_version": dataset_manifest.get("dataset_version"),
            "training_config": cfg.model_dump(mode="json"),
            "splits": split_summary,
            "decision_threshold": {
                "value": threshold,
                "method": f"max F{cfg.evaluation.threshold_fbeta:g} on validation",
            },
            "validation_operating_points": val_ops,
            "validation_metrics": validation[selected],
            "test_metrics_summary": {
                "average_precision": test_report["average_precision"],
                "roc_auc": test_report["roc_auc"],
                "precision": test_report["at_validation_threshold"]["precision"],
                "recall": test_report["at_validation_threshold"]["recall"],
            },
            "environment": {
                "python": platform.python_version(),
                "xgboost": xgb.__version__,
                "sklearn": sklearn.__version__,
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "machine": platform.machine(),
            },
        },
    )
    report = {
        "model_name": cfg.model_name,
        "model_version": cfg.model_version,
        "dataset_version": dataset_manifest.get("dataset_version"),
        "model_sha256": manifest["model_sha256"],
        "splits": split_summary,
        "validation": validation,
        "best_on_validation": best_overall,
        "selected_for_serving": selected,
        "selection_note": (
            "Selected = highest validation AP among servable candidates; candidates within "
            f"{cfg.evaluation.selection_ap_tolerance} AP are tied and broken by validation Brier."
            + (
                ""
                if best_overall == selected
                else f" '{best_overall}' had the highest raw validation AP"
                f" ({'servable' if best_overall in SERVABLE else 'not servable'})."
            )
        ),
        "decision_threshold": manifest["decision_threshold"],
        "validation_operating_points": val_ops,
        "test": test_report,
        "test_posthoc_all_candidates": posthoc,
        "elapsed_seconds": time.perf_counter() - t0,
    }
    (out_dir / "evaluation.json").write_text(json.dumps(report, indent=2, default=float))
    return report


def load_config(path: Path) -> TrainConfig:
    return TrainConfig.model_validate(yaml.safe_load(path.read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Train card fraud models")
    parser.add_argument("--config", type=Path, default=Path("ml/configs/train_card_v1.yaml"))
    parser.add_argument(
        "--data", type=Path, default=Path("ml/data/processed/card_transactions.parquet")
    )
    parser.add_argument("--artifacts", type=Path, default=Path("ml/artifacts"))
    args = parser.parse_args()
    cfg = load_config(args.config)
    dataset_manifest = json.loads((args.data.parent / "dataset_manifest.json").read_text())
    events = pd.read_parquet(args.data)
    out_dir = args.artifacts / f"{cfg.model_name}-{cfg.model_version}"
    report = run_training(cfg, events, out_dir, dataset_manifest)
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("selected_for_serving", "selection_note", "decision_threshold")
            },
            indent=2,
        )
    )
    print(f"artifact: {out_dir}")


if __name__ == "__main__":
    main()
