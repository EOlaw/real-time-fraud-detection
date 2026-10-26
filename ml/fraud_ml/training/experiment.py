"""Phase 3 experiment: tuning, imbalance comparison, selection, explanations, anomaly baseline.

Usage:
    uv run python -m fraud_ml.training.experiment --config ml/configs/experiment_card_v2.yaml

Order of operations (the held-out test window is read only in step 6):
  1. Point-in-time features over the full event stream (features only look back).
  2. Chronological CV folds strictly inside the training window (``cv.py``).
  3. Optuna search on mean fold AP (``tuning.py``); incumbent parameters enqueued as trial 0.
  4. Imbalance strategies (none / class_weight / undersample / smotenc) with the tuned parameters:
     CV on the folds, then refit on the full training window. Resampling only ever receives
     training rows.
  5. Selection on the *validation* window with the Phase 1 rule (best AP; within tolerance,
     lowest Brier), with the incumbent artifact as a candidate. A new model gets a new version,
     a threshold chosen on validation, and a candidate policy bound to that version.
  6. Test window: selected model and incumbent scored once, for reporting only.
  7. SHAP explanations of the selected model; separate unsupervised anomaly baseline.
Nothing is promoted: serving still uses ``ml/artifacts/current`` and
``ml/configs/decision_policy.yaml`` until ``fraud_api.cli promote-model`` is run.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import optuna
import pandas as pd
import sklearn
import xgboost as xgb
import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from scipy.stats import spearmanr
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

from fraud_ml.anomaly.isolation import ANOMALY_FEATURES, BehavioralAnomalyDetector
from fraud_ml.domains.card_payments import DOMAIN
from fraud_ml.evaluation.metrics import (
    best_fbeta_threshold,
    calibration_table,
    cluster_bootstrap_ci,
    operating_points,
    paired_cluster_bootstrap_diff,
    ranking_metrics,
    threshold_metrics,
    top_k_metrics,
)
from fraud_ml.explain.treeshap import UNITS, explain_rows, global_importance, tree_shap
from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import FEATURE_SPEC_VERSION, MODEL_COLUMNS, NUMERIC_FEATURES
from fraud_ml.models.artifact import load_artifact, save_artifact
from fraud_ml.tracking import MlflowTracker, RecordingTracker, Tracker, flatten, guard
from fraud_ml.training.cv import Fold, chronological_folds
from fraud_ml.training.resampling import STRATEGIES, ResampleConfig, resample
from fraud_ml.training.train import (
    EvaluationConfig,
    SplitConfig,
    _git_commit,
    build_matrix,
    select_model,
    split_masks,
)
from fraud_ml.training.tuning import ParamSpec, ResampleFn, evaluate_fold, fit_xgb, tune

FEATURE_COLUMNS = [*NUMERIC_FEATURES, "category", "channel"]
_AP = average_precision_score


class ExperimentError(RuntimeError):
    pass


class CVConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    boundaries: list[datetime]
    embargo_days: float


class TuningConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    # The incumbent's parameters are enqueued as trial 0; requiring them to lie inside the search
    # space makes trial 0 a faithful baseline instead of an out-of-range fixed trial.
    require_incumbent_in_space: bool = True
    n_trials: int
    sampler_seed: int
    pruner_startup_trials: int
    search_space: dict[str, ParamSpec]


class ImbalanceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    undersample_ratio: float
    smote_ratio: float
    smote_k_neighbors: int


class ExplainConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    global_sample_rows: int
    examples_per_kind: int


class AnomalyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    n_estimators: int
    max_samples: int
    fit_sample_rows: int


class MlflowConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool
    tracking_uri: str
    experiment: str
    artifact_root: Path


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(frozen=True, protected_namespaces=())
    model_name: str
    candidate_version: str
    seed: int
    baseline_artifact: Path
    splits: SplitConfig
    cv: CVConfig
    tuning: TuningConfig
    imbalance: ImbalanceConfig
    evaluation: EvaluationConfig
    explain: ExplainConfig
    anomaly: AnomalyConfig
    mlflow: MlflowConfig
    policy_out_dir: Path

    @model_validator(mode="after")
    def _policy_operating_points(self) -> ExperimentConfig:
        # Candidate policies use the same derivation as the incumbent policy (see
        # candidate_policy), which needs these two validation alert budgets.
        missing = {0.0025, 0.01} - set(self.evaluation.alert_rates)
        if missing:
            raise ValueError(f"evaluation.alert_rates must include {sorted(missing)}")
        return self


def baseline_params_for(cfg: ExperimentConfig) -> dict[str, Any]:
    """XGBoost parameters of the incumbent, read from its hash-verified manifest."""
    params = load_artifact(cfg.baseline_artifact).manifest["training_config"]["models"]["xgboost"]
    return dict(params)


def load_config(path: Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(yaml.safe_load(path.read_text()))


def _logit(p: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    q = np.clip(p, 1e-7, 1 - 1e-7)
    return np.asarray(np.log(q / (1 - q)), dtype=np.float64)


def _validation_metrics(
    y: npt.NDArray[np.int64],
    p: npt.NDArray[np.float64],
    p_baseline: npt.NDArray[np.float64],
    groups: npt.NDArray[Any],
    cfg: ExperimentConfig,
) -> dict[str, Any]:
    k = round(cfg.evaluation.review_capacity_fraction * len(y))
    cal = calibration_table(y, p)
    n, seed = cfg.evaluation.bootstrap_resamples, cfg.seed
    return {
        **ranking_metrics(y, p),
        **top_k_metrics(y, p, k),
        "brier": cal["brier"],
        "expected_calibration_error": cal["expected_calibration_error"],
        "average_precision_ci": cluster_bootstrap_ci(
            y, p, groups, lambda a, b: float(_AP(a, b)), n_resamples=n, seed=seed
        ),
        "average_precision_minus_baseline": paired_cluster_bootstrap_diff(
            y, p, p_baseline, groups, lambda a, b: float(_AP(a, b)), n_resamples=n, seed=seed
        ),
    }


def _test_report(
    y: npt.NDArray[np.int64],
    p: npt.NDArray[np.float64],
    threshold: float,
    groups: npt.NDArray[Any],
    channels: npt.NDArray[Any],
    cfg: ExperimentConfig,
) -> dict[str, Any]:
    n, seed = cfg.evaluation.bootstrap_resamples, cfg.seed
    return {
        **ranking_metrics(y, p),
        "at_validation_threshold": threshold_metrics(y, p, threshold),
        "top_k": top_k_metrics(y, p, round(cfg.evaluation.review_capacity_fraction * len(y))),
        "average_precision_ci": cluster_bootstrap_ci(
            y, p, groups, lambda a, b: float(_AP(a, b)), n_resamples=n, seed=seed
        ),
        "recall_at_threshold_ci": cluster_bootstrap_ci(
            y,
            p,
            groups,
            lambda a, b: threshold_metrics(a, b, threshold)["recall"],
            n_resamples=n,
            seed=seed,
        ),
        "precision_at_threshold_ci": cluster_bootstrap_ci(
            y,
            p,
            groups,
            lambda a, b: threshold_metrics(a, b, threshold)["precision"],
            n_resamples=n,
            seed=seed,
        ),
        "calibration": calibration_table(y, p),
        "segments_by_channel": {
            str(ch): {
                **ranking_metrics(y[channels == ch], p[channels == ch]),
                **threshold_metrics(y[channels == ch], p[channels == ch], threshold),
            }
            for ch in np.unique(channels)
            if y[channels == ch].sum() > 0
        },
    }


def candidate_policy(
    model_name: str, version: str, review_at: float, ops: list[dict[str, float]]
) -> dict[str, Any]:
    """Thresholds derived exactly as for the incumbent policy: challenge at the ~1% validation
    alert-rate threshold, review at max-F2 on validation, decline at ~0.25% alert rate."""
    by_rate = {op["target_alert_rate"]: op["threshold"] for op in ops}
    challenge = min(by_rate[0.01], review_at)
    decline = max(by_rate[0.0025], review_at)
    return {
        "version": f"card-policy-{model_name}-{version}",
        "domain": DOMAIN,
        "mode": "sandbox",
        "model": {"name": model_name, "version": version},
        "thresholds": {
            "challenge_at": round(float(challenge), 6),
            "review_at": round(float(review_at), 6),
            "decline_at": round(float(decline), 6),
        },
        "on_scoring_failure": "review",
    }


def _anomaly_analysis(
    cfg: ExperimentConfig,
    features: pd.DataFrame,
    masks: dict[str, npt.NDArray[np.bool_]],
    y: npt.NDArray[np.int64],
    p_val: npt.NDArray[np.float64],
    p_test: npt.NDArray[np.float64],
    threshold: float,
    groups_test: npt.NDArray[Any],
) -> dict[str, Any]:
    rng = np.random.default_rng(cfg.seed)
    train_pos = np.flatnonzero(masks["train"])
    fit_rows = np.sort(
        rng.choice(train_pos, min(cfg.anomaly.fit_sample_rows, len(train_pos)), replace=False)
    )
    detector = BehavioralAnomalyDetector(
        cfg.anomaly.n_estimators, cfg.anomaly.max_samples, cfg.seed
    ).fit(features.iloc[fit_rows][list(ANOMALY_FEATURES)])
    out: dict[str, Any] = {
        "detector": "isolation_forest",
        "features": list(ANOMALY_FEATURES),
        "fit_rows": len(fit_rows),
        "fit_window": "training window sample, labels not used",
        "units": "unbounded anomaly score, higher = more anomalous; not a probability",
    }
    scores = {}
    for name, p in (("validation", p_val), ("test", p_test)):
        m = masks[name]
        a = detector.score(features.loc[m, list(ANOMALY_FEATURES)]).values
        scores[name] = a
        yy = y[m]
        k = round(cfg.evaluation.review_capacity_fraction * len(yy))
        top_a = np.argsort(-a, kind="stable")[:k]
        missed = (p < threshold) & (yy == 1)
        out[name] = {
            **ranking_metrics(yy, a),
            **top_k_metrics(yy, a, k),
            "spearman_with_fraud_probability": float(spearmanr(a, p).statistic),
            "frauds_missed_by_model_at_threshold": int(missed.sum()),
            "of_which_in_anomaly_top_k": int(missed[top_a].sum()),
        }
    # Analysis only: does the anomaly score add ranking signal on top of the fraud model? A
    # logistic combiner is fit on validation and scored on test. It is NOT a served model.
    xv = np.column_stack([_logit(p_val), scores["validation"]])
    xt = np.column_stack([_logit(p_test), scores["test"]])
    combiner = LogisticRegression(max_iter=1000).fit(xv, y[masks["validation"]])
    stacked = combiner.predict_proba(xt)[:, 1]
    out["analysis_only_stacked_on_test"] = {
        "note": "logistic combiner fit on validation; not deployed; anomaly stays separate",
        "average_precision_stacked": float(_AP(y[masks["test"]], stacked)),
        "average_precision_model_alone": float(_AP(y[masks["test"]], p_test)),
        "stacked_minus_model": paired_cluster_bootstrap_diff(
            y[masks["test"]],
            stacked,
            p_test,
            groups_test,
            lambda a, b: float(_AP(a, b)),
            n_resamples=cfg.evaluation.bootstrap_resamples,
            seed=cfg.seed,
        ),
    }
    return out


def run_experiment(
    cfg: ExperimentConfig,
    events: pd.DataFrame,
    artifacts_root: Path,
    dataset_manifest: dict[str, Any],
    tracker: Tracker,
    log: Callable[[str], None] = print,
    resample_fn: ResampleFn | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    resample_fn = resample_fn or resample
    rcfg = ResampleConfig(**cfg.imbalance.model_dump())
    seed = cfg.seed

    events = events.sort_values(["event_time", "event_id"], kind="mergesort").reset_index(drop=True)
    features = compute_offline_features(events)
    y = events["is_fraud"].to_numpy(np.int64)
    masks = split_masks(events, cfg.splits)
    train_pos = np.flatnonzero(masks["train"])
    local = chronological_folds(
        events["event_time"].iloc[train_pos].reset_index(drop=True),
        cfg.splits.warmup_end,
        cfg.splits.train_end,
        cfg.cv.boundaries,
        cfg.cv.embargo_days,
    )
    folds: list[Fold] = [
        replace(f, train_idx=train_pos[f.train_idx], val_idx=train_pos[f.val_idx]) for f in local
    ]
    for f in folds:  # defensive: every fold row is a training-window row
        if not (masks["train"][f.train_idx].all() and masks["train"][f.val_idx].all()):
            raise ExperimentError("fold rows outside the training window")
    outside = (
        {
            k: v
            for k, v in baseline_params_for(cfg).items()
            if k in cfg.tuning.search_space
            and not cfg.tuning.search_space[k].low <= float(v) <= cfg.tuning.search_space[k].high
        }
        if cfg.tuning.require_incumbent_in_space
        else {}
    )
    if outside:
        raise ExperimentError(f"incumbent parameters outside the search space: {outside}")
    log(f"features {features.shape}; folds {[f.describe() for f in folds]}")

    baseline = load_artifact(cfg.baseline_artifact)  # hash-verified, contract-checked
    if baseline.name != cfg.model_name:
        raise ExperimentError("baseline artifact belongs to a different model name")
    baseline_params = baseline.manifest["training_config"]["models"]["xgboost"]
    lineage = {
        "dataset_version": str(dataset_manifest.get("dataset_version")),
        "canonical_schema_version": str(dataset_manifest.get("canonical_schema_version", "1")),
        "dataset_parquet_sha256": str(dataset_manifest.get("output", {}).get("sha256")),
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "baseline_model": f"{baseline.name}@{baseline.version}",
        "baseline_sha256": baseline.sha256,
        "git_commit": _git_commit(),
    }

    with tracker.run(f"phase3:{cfg.model_name}@{cfg.candidate_version}"):
        tracker.params(flatten(cfg.model_dump(mode="json")))
        tracker.tags(lineage)
        tracker.artifact_dict({"folds": [f.describe() for f in folds]}, "folds.json")

        # ---- 3. tuning (training window only) -------------------------------------------
        trials: list[dict[str, Any]] = []
        with tracker.run("tuning", nested=True):

            def on_trial(t: optuna.trial.FrozenTrial) -> None:
                row = {
                    "number": t.number,
                    "state": t.state.name,
                    "value": t.value,
                    "params": t.params,
                    **t.user_attrs,
                }
                trials.append(row)
                metrics = {k: v for k, v in t.user_attrs.items() if isinstance(v, float)}
                if t.value is not None:
                    metrics["mean_cv_average_precision"] = t.value
                tracker.metrics(metrics, step=t.number)

            study = tune(
                folds,
                features,
                y,
                cfg.tuning.search_space,
                cfg.tuning.n_trials,
                cfg.tuning.sampler_seed,
                seed,
                build_matrix,
                rcfg,
                initial_params=baseline_params,
                pruner_startup_trials=cfg.tuning.pruner_startup_trials,
                on_trial=on_trial,
                resample_fn=resample_fn,
            )
            best_params = {**baseline_params, **study.best_params}
            tracker.params({f"best.{k}": v for k, v in best_params.items()})
            tracker.metrics({"best_mean_cv_average_precision": study.best_value})
            tracker.artifact_dict({"trials": trials}, "trials.json")
        log(
            f"tuning: best mean CV AP {study.best_value:.4f} (trial {study.best_trial.number})"
            f"; incumbent params {trials[0]['value']}"
        )

        # ---- 4. imbalance strategies: CV, then refit on full training window ---------------
        cv_results: dict[str, Any] = {}
        val_scores: dict[str, npt.NDArray[np.float64]] = {}
        fitted: dict[str, xgb.XGBClassifier] = {}
        resampling_summary: dict[str, Any] = {}
        x_val = build_matrix(features.loc[masks["validation"]])
        for strategy in STRATEGIES:
            with tracker.run(f"cv:{strategy}", nested=True):
                per_fold = []
                for f in folds:
                    m = evaluate_fold(
                        f, features, y, best_params, strategy, rcfg, seed, build_matrix, resample_fn
                    )
                    per_fold.append(m)
                    tracker.metrics(m, step=f.index)
                aps = [m["average_precision"] for m in per_fold]
                briers = [m["brier"] for m in per_fold]
                cv_results[strategy] = {
                    "folds": per_fold,
                    "mean_average_precision": float(np.mean(aps)),
                    "std_average_precision": float(np.std(aps, ddof=1)) if len(aps) > 1 else 0.0,
                    "mean_brier": float(np.mean(briers)),
                }
                tracker.metrics({k: v for k, v in cv_results[strategy].items() if k != "folds"})
            rs = resample_fn(strategy, features.iloc[train_pos], y[train_pos], rcfg, seed)
            fitted[strategy] = fit_xgb(
                best_params, build_matrix(rs.features), rs.y, seed, rs.xgb_params
            )
            resampling_summary[strategy] = {
                "train_rows": len(rs.y),
                "positives": int(rs.y.sum()),
                "synthetic_rows": rs.synthetic_rows,
                "dropped_rows": rs.dropped_rows,
                "xgb_params": rs.xgb_params,
            }
            val_scores[f"tuned:{strategy}"] = (
                fitted[strategy].predict_proba(x_val)[:, 1].astype(np.float64)
            )
            log(f"{strategy}: CV AP {cv_results[strategy]['mean_average_precision']:.4f}")

        # ---- 5. selection on validation ---------------------------------------------------
        y_val = y[masks["validation"]]
        g_val = events.loc[masks["validation"], "customer_id"].to_numpy()
        baseline_key = f"baseline:{baseline.name}@{baseline.version}"
        p_base_val = baseline.predict_proba(x_val)
        val_scores[baseline_key] = p_base_val
        recorded = baseline.manifest["validation_metrics"]["average_precision"]
        reproduced = math.isclose(float(_AP(y_val, p_base_val)), recorded, rel_tol=1e-9)
        validation = {}
        for name, p in val_scores.items():
            with tracker.run(f"validation:{name}", nested=True):
                validation[name] = _validation_metrics(y_val, p, p_base_val, g_val, cfg)
                tracker.metrics({k: v for k, v in validation[name].items() if isinstance(v, float)})
        selected = select_model(
            validation, cfg.evaluation.selection_ap_tolerance, candidates=validation.keys()
        )
        new_model = selected != baseline_key
        log(f"selected: {selected}")

        version = cfg.candidate_version if new_model else baseline.version
        if new_model:
            chosen_strategy = selected.split(":", 1)[1]
            p_sel_val = val_scores[selected]
            threshold = best_fbeta_threshold(y_val, p_sel_val, beta=cfg.evaluation.threshold_fbeta)
            ops = operating_points(y_val, p_sel_val, cfg.evaluation.alert_rates)
            booster = fitted[chosen_strategy].get_booster()
        else:
            chosen_strategy = "incumbent"
            threshold = float(baseline.manifest["decision_threshold"]["value"])
            ops = baseline.manifest["validation_operating_points"]
            booster = baseline.booster
        booster.feature_names = list(MODEL_COLUMNS)

        # ---- 6. held-out test: read only now, after every decision above -----------------
        x_test = build_matrix(features.loc[masks["test"]])
        y_test = y[masks["test"]]
        g_test = events.loc[masks["test"], "customer_id"].to_numpy()
        ch_test = features.loc[masks["test"], "channel"].to_numpy()
        p_base_test = baseline.predict_proba(x_test)
        dm_test = xgb.DMatrix(x_test, feature_names=list(MODEL_COLUMNS), missing=np.nan)
        p_sel_test = booster.predict(dm_test).astype(np.float64)
        test = {
            "selected": _test_report(y_test, p_sel_test, threshold, g_test, ch_test, cfg),
            "baseline": _test_report(
                y_test,
                p_base_test,
                float(baseline.manifest["decision_threshold"]["value"]),
                g_test,
                ch_test,
                cfg,
            ),
            "selected_minus_baseline_average_precision": paired_cluster_bootstrap_diff(
                y_test,
                p_sel_test,
                p_base_test,
                g_test,
                lambda a, b: float(_AP(a, b)),
                n_resamples=cfg.evaluation.bootstrap_resamples,
                seed=seed,
            ),
            "posthoc_all_candidates": {
                **{
                    f"tuned:{s}": ranking_metrics(y_test, m.predict_proba(x_test)[:, 1])
                    for s, m in fitted.items()
                },
                baseline_key: ranking_metrics(y_test, p_base_test),
            },
        }
        with tracker.run("test", nested=True):
            tracker.metrics(
                {
                    "selected_average_precision": test["selected"]["average_precision"],
                    "baseline_average_precision": test["baseline"]["average_precision"],
                    "selected_recall": test["selected"]["at_validation_threshold"]["recall"],
                    "selected_precision": test["selected"]["at_validation_threshold"]["precision"],
                    "selected_brier": test["selected"]["calibration"]["brier"],
                }
            )

        # ---- 7a. explanations of the selected model --------------------------------------
        rng = np.random.default_rng(seed)
        val_pos = np.flatnonzero(masks["validation"])
        sample = np.sort(
            rng.choice(val_pos, min(cfg.explain.global_sample_rows, len(val_pos)), replace=False)
        )
        contrib = tree_shap(booster, build_matrix(features.iloc[sample]))
        positives = val_pos[y_val == 1]
        contrib_pos = tree_shap(booster, build_matrix(features.iloc[positives]))
        p_sel_val_all = booster.predict(
            xgb.DMatrix(x_val, feature_names=list(MODEL_COLUMNS), missing=np.nan)
        ).astype(float)
        order = np.argsort(-p_sel_val_all, kind="stable")
        n_ex = cfg.explain.examples_per_kind
        legit_by_score = [int(i) for i in order[::-1] if y_val[i] == 0]
        low_pool = legit_by_score[: max(n_ex, len(legit_by_score) // 100)]
        picks = {
            "high_score_fraud": [i for i in order if y_val[i] == 1][:n_ex],
            "high_score_legitimate": [i for i in order if y_val[i] == 0][:n_ex],
            # Random rows from the lowest-scored 1% of legitimate transactions.
            "low_score_legitimate": [int(i) for i in rng.choice(low_pool, n_ex, replace=False)],
        }
        ex_rows = [val_pos[i] for kind in picks.values() for i in kind]
        ex_labels = [
            f"validation_{kind}_{j + 1}" for kind, rows in picks.items() for j in range(len(rows))
        ]
        ex_contrib = tree_shap(booster, build_matrix(features.iloc[ex_rows]))
        explanations: dict[str, Any] = {
            "model": f"{cfg.model_name}@{version}",
            "method": "XGBoost TreeSHAP (pred_contribs), exact path-dependent",
            "units": UNITS,
            "global_sample": {"rows": len(sample), "window": "validation, random sample"},
            "max_reconstruction_error": max(
                contrib.reconstruction_error,
                contrib_pos.reconstruction_error,
                ex_contrib.reconstruction_error,
            ),
            "global_importance": global_importance(contrib),
            "global_importance_fraud_rows": global_importance(contrib_pos),
            "examples": explain_rows(
                ex_contrib, features.iloc[ex_rows][FEATURE_COLUMNS], ex_labels
            ),
        }
        with tracker.run("explanations", nested=True):
            tracker.metrics(
                {"max_reconstruction_error": float(explanations["max_reconstruction_error"])}
            )
            tracker.artifact_dict(
                {k: v for k, v in explanations.items() if k != "examples"}, "global_importance.json"
            )

        # ---- 7b. anomaly baseline (separate score; never enters selection or policy) -----
        anomaly: dict[str, Any] = {"enabled": False}
        if cfg.anomaly.enabled:
            anomaly = _anomaly_analysis(
                cfg, features, masks, y, p_sel_val_all, p_sel_test, threshold, g_test
            )
            with tracker.run("anomaly", nested=True):
                tracker.metrics(
                    {
                        f"{w}_{k}": v
                        for w in ("validation", "test")
                        for k, v in anomaly[w].items()
                        if isinstance(v, float)
                    }
                )

        # ---- outputs -----------------------------------------------------------------------
        split_summary = {
            name: {"rows": int(m.sum()), "positives": int(y[m].sum())} for name, m in masks.items()
        }
        artifact_dir: Path | None = None
        policy_path: Path | None = None
        manifest: dict[str, Any] | None = None
        if new_model:
            artifact_dir = artifacts_root / f"{cfg.model_name}-{version}"
            policy_path = cfg.policy_out_dir / f"{cfg.model_name}-{version}.yaml"
            if artifact_dir.exists() or policy_path.exists():
                raise ExperimentError(
                    f"refusing to overwrite existing {artifact_dir} / "
                    f"{policy_path}; bump candidate_version"
                )
            manifest = save_artifact(
                booster,
                artifact_dir,
                {
                    "model_name": cfg.model_name,
                    "model_version": version,
                    "domain": DOMAIN,
                    "algorithm": f"xgboost (imbalance strategy: {chosen_strategy})",
                    "created_at": datetime.now(UTC).isoformat(),
                    "git_commit": lineage["git_commit"],
                    "dataset_version": lineage["dataset_version"],
                    "lineage": {
                        "parent_model": lineage["baseline_model"],
                        "parent_sha256": baseline.sha256,
                        "experiment": "phase3",
                        "tuned_params": best_params,
                        "imbalance_strategy": chosen_strategy,
                    },
                    "training_config": {
                        "models": {"xgboost": best_params},
                        "experiment": cfg.model_dump(mode="json"),
                    },
                    "splits": split_summary,
                    "decision_threshold": {
                        "value": threshold,
                        "method": f"max F{cfg.evaluation.threshold_fbeta:g} on validation",
                    },
                    "validation_operating_points": ops,
                    "validation_metrics": {
                        k: v for k, v in validation[selected].items() if isinstance(v, float)
                    },
                    "test_metrics_summary": {
                        "average_precision": test["selected"]["average_precision"],
                        "roc_auc": test["selected"]["roc_auc"],
                        "precision": test["selected"]["at_validation_threshold"]["precision"],
                        "recall": test["selected"]["at_validation_threshold"]["recall"],
                    },
                    "environment": {
                        "python": platform.python_version(),
                        "xgboost": xgb.__version__,
                        "sklearn": sklearn.__version__,
                        "optuna": optuna.__version__,
                    },
                },
            )
            policy = candidate_policy(cfg.model_name, version, threshold, ops)
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(
                "# Candidate decision policy generated by fraud_ml.training.experiment.\n"
                "# Thresholds come from validation operating points of the bound model.\n"
                "# NOT active: promote with `fraud_api.cli promote-model` after gates pass.\n"
                + yaml.safe_dump(policy, sort_keys=False)
            )
            (artifact_dir / "explanations.json").write_text(
                json.dumps(explanations, indent=2, default=float)
            )
            with tracker.run("selection", nested=True):
                tracker.tags(
                    {
                        "selected": selected,
                        "new_model_version": version,
                        "artifact_sha256": manifest["model_sha256"],
                        "candidate_policy": policy["version"],
                    }
                )
                tracker.artifact_file(artifact_dir / "manifest.json")
                tracker.artifact_file(policy_path)
        else:
            with tracker.run("selection", nested=True):
                tracker.tags({"selected": selected, "new_model_version": "none"})

        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "lineage": lineage,
            "splits": split_summary,
            "folds": [f.describe() for f in folds],
            "tuning": {
                "n_trials": cfg.tuning.n_trials,
                "completed": sum(t["state"] == "COMPLETE" for t in trials),
                "pruned": sum(t["state"] == "PRUNED" for t in trials),
                "incumbent_params_mean_cv_average_precision": trials[0]["value"],
                "best_trial": study.best_trial.number,
                "best_mean_cv_average_precision": study.best_value,
                "best_params": best_params,
                "trials": trials,
            },
            "imbalance": {"cv": cv_results, "resampling": resampling_summary},
            "validation": validation,
            "baseline_reproduced_on_validation": reproduced,
            "selection": {
                "rule": "highest validation AP; candidates within "
                f"{cfg.evaluation.selection_ap_tolerance} AP tie, broken by lowest "
                "validation Brier",
                "selected": selected,
                "new_model": new_model,
                "model_version": version,
                "decision_threshold": threshold,
                "validation_operating_points": ops,
                "artifact_sha256": manifest["model_sha256"] if manifest else baseline.sha256,
                "candidate_policy": str(policy_path) if policy_path else None,
                "promoted": False,
            },
            "test": test,
            "explanations": {k: v for k, v in explanations.items() if k != "examples"},
            "anomaly": anomaly,
            "elapsed_seconds": time.perf_counter() - started,
        }
        guard(report)  # aggregate report: no event ids, customer tokens or secrets
        with tracker.run("report", nested=True):
            tracker.artifact_dict(report, "experiment_report.json")
        report["artifact_dir"] = str(artifact_dir) if artifact_dir else None
        report["baseline_val_scores_reproduced"] = reproduced
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 card-fraud experiment")
    parser.add_argument("--config", type=Path, default=Path("ml/configs/experiment_card_v2.yaml"))
    parser.add_argument(
        "--data", type=Path, default=Path("ml/data/processed/card_transactions.parquet")
    )
    parser.add_argument("--artifacts", type=Path, default=Path("ml/artifacts"))
    parser.add_argument("--reports", type=Path, default=Path("ml/reports/phase3"))
    args = parser.parse_args()
    cfg = load_config(args.config)
    dataset_manifest = json.loads((args.data.parent / "dataset_manifest.json").read_text())
    tracker: Tracker = (
        MlflowTracker(cfg.mlflow.tracking_uri, cfg.mlflow.experiment, cfg.mlflow.artifact_root)
        if cfg.mlflow.enabled
        else RecordingTracker()
    )
    report = run_experiment(
        cfg,
        pd.read_parquet(args.data),
        args.artifacts,
        dataset_manifest,
        tracker,
        log=lambda m: print(m, flush=True),
    )
    args.reports.mkdir(parents=True, exist_ok=True)
    out = args.reports / f"{cfg.model_name}-{cfg.candidate_version}-experiment.json"
    out.write_text(json.dumps(report, indent=2, default=float))
    print(json.dumps(report["selection"], indent=2, default=float))
    print(f"report: {out}")


if __name__ == "__main__":
    main()
