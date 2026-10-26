"""Optuna hyperparameter search scored by chronological CV inside the training window.

Reproducibility: TPE sampler with a fixed seed, trials run sequentially (``n_jobs=1``), fixed
XGBoost seed, explicit fold boundaries, and the incumbent model's parameters enqueued as trial 0 so
the search is always compared against the baseline configuration. Folds are reported to the
median pruner one at a time; pruning decisions are deterministic given the same history.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import optuna
import pandas as pd
import xgboost as xgb
from pydantic import BaseModel, ConfigDict
from sklearn.metrics import average_precision_score, brier_score_loss

from fraud_ml.training.cv import Fold
from fraud_ml.training.resampling import ResampleConfig, Resampled, Strategy, resample

ResampleFn = Callable[
    [Strategy, pd.DataFrame, npt.NDArray[np.int64], ResampleConfig, int], Resampled
]
MatrixFn = Callable[[pd.DataFrame], npt.NDArray[np.float32]]


class ParamSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: Literal["int", "float"]
    low: float
    high: float
    log: bool = False
    step: float | None = None


def suggest(trial: optuna.Trial, space: Mapping[str, ParamSpec]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, spec in sorted(space.items()):
        if spec.type == "int":
            params[name] = trial.suggest_int(
                name, int(spec.low), int(spec.high), step=int(spec.step or 1), log=spec.log
            )
        else:
            params[name] = trial.suggest_float(
                name, spec.low, spec.high, log=spec.log, step=spec.step
            )
    return params


def fit_xgb(
    params: Mapping[str, Any],
    x: npt.NDArray[np.float32],
    y: npt.NDArray[np.int64],
    seed: int,
    extra: Mapping[str, Any] | None = None,
) -> xgb.XGBClassifier:
    model = xgb.XGBClassifier(
        **params,
        **(extra or {}),
        tree_method="hist",
        objective="binary:logistic",
        eval_metric="aucpr",
        random_state=seed,
        n_jobs=-1,
    )
    model.fit(x, y)
    return model


def evaluate_fold(
    fold: Fold,
    features: pd.DataFrame,
    y: npt.NDArray[np.int64],
    params: Mapping[str, Any],
    strategy: Strategy,
    rcfg: ResampleConfig,
    seed: int,
    to_matrix: MatrixFn,
    resample_fn: ResampleFn = resample,
) -> dict[str, float]:
    """Resample the fold's *training* rows only, fit, and score the untouched validation rows."""
    tr = features.iloc[fold.train_idx]
    rs = resample_fn(strategy, tr, y[fold.train_idx], rcfg, seed)
    model = fit_xgb(params, to_matrix(rs.features), rs.y, seed, rs.xgb_params)
    yv = y[fold.val_idx]
    p = model.predict_proba(to_matrix(features.iloc[fold.val_idx]))[:, 1]
    if yv.sum() == 0:
        raise ValueError(f"fold {fold.index} validation block has no positive labels")
    return {
        "average_precision": float(average_precision_score(yv, p)),
        "brier": float(brier_score_loss(yv, p)),
        "train_rows_after_resampling": float(len(rs.y)),
        "validation_rows": float(len(yv)),
        "validation_positives": float(yv.sum()),
    }


def tune(
    folds: Sequence[Fold],
    features: pd.DataFrame,
    y: npt.NDArray[np.int64],
    space: Mapping[str, ParamSpec],
    n_trials: int,
    sampler_seed: int,
    model_seed: int,
    to_matrix: MatrixFn,
    rcfg: ResampleConfig,
    initial_params: Mapping[str, Any] | None = None,
    pruner_startup_trials: int = 5,
    on_trial: Callable[[optuna.trial.FrozenTrial], None] | None = None,
    resample_fn: ResampleFn = resample,
) -> optuna.Study:
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=sampler_seed),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=pruner_startup_trials),
    )
    if initial_params is not None:
        study.enqueue_trial({k: v for k, v in initial_params.items() if k in space})

    def objective(trial: optuna.Trial) -> float:
        params = suggest(trial, space)
        scores = []
        for fold in folds:
            m = evaluate_fold(
                fold, features, y, params, "none", rcfg, model_seed, to_matrix, resample_fn
            )
            scores.append(m["average_precision"])
            trial.set_user_attr(f"fold_{fold.index}_average_precision", m["average_precision"])
            trial.report(float(np.mean(scores)), step=fold.index)
            if trial.should_prune():
                raise optuna.TrialPruned()
        return float(np.mean(scores))

    callbacks = [lambda _s, t: on_trial(t)] if on_trial else None
    study.optimize(objective, n_trials=n_trials, n_jobs=1, callbacks=callbacks)
    return study
