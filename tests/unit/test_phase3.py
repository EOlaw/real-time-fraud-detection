"""Phase 3: chronological tuning, imbalance strategies, explanations, anomaly separation,
tracking hygiene and promotion gates. All runs use the synthetic fixture, not the real dataset."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from fraud_api.promotion import apply_promotion, check_promotion
from fraud_api.scoring.policy import DecisionPolicy
from fraud_ml.anomaly.isolation import (
    ANOMALY_FEATURES,
    AnomalyInputError,
    AnomalyScores,
    BehavioralAnomalyDetector,
)
from fraud_ml.data.ingest import MANIFEST_FILE, sha256_file
from fraud_ml.domains.card_payments import CHANNELS, MERCHANT_CATEGORIES, derive_channel
from fraud_ml.explain.treeshap import ExplanationError, explain_rows, tree_shap
from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import MODEL_COLUMNS, NUMERIC_FEATURES
from fraud_ml.models.artifact import load_artifact
from fraud_ml.tracking import RecordingTracker, SensitiveDataError, guard
from fraud_ml.training import experiment as experiment_module
from fraud_ml.training.cv import FoldError, chronological_folds
from fraud_ml.training.experiment import (
    ExperimentConfig,
    ExperimentError,
    run_experiment,
)
from fraud_ml.training.resampling import ResampleConfig, Resampled, resample
from fraud_ml.training.train import build_matrix, run_training, split_masks
from fraud_ml.training.tuning import ParamSpec, tune
from tests.conftest import small_train_config
from tests.fixtures import synthetic_events

SECRET = "phase3-test-secret-value"


def events() -> pd.DataFrame:
    return synthetic_events(n=6_000, n_customers=40, seed=11)


def config(tmp: Path, baseline: Path, **overrides: Any) -> ExperimentConfig:
    base = small_train_config()
    data: dict[str, Any] = {
        "model_name": "test-model",
        "candidate_version": "0.1.0",
        "seed": 1,
        "baseline_artifact": str(baseline),
        "splits": base.splits.model_dump(),
        "cv": {
            "boundaries": ["2020-01-25T00:00:00Z", "2020-02-05T00:00:00Z", "2020-02-20T00:00:00Z"],
            "embargo_days": 1,
        },
        "tuning": {
            # The fixture's weak incumbent is deliberately outside this small search space.
            "require_incumbent_in_space": False,
            "n_trials": 4,
            "sampler_seed": 3,
            "pruner_startup_trials": 2,
            "search_space": {
                "n_estimators": {"type": "int", "low": 20, "high": 60, "step": 20},
                "max_depth": {"type": "int", "low": 2, "high": 4},
                "learning_rate": {"type": "float", "low": 0.05, "high": 0.3, "log": True},
            },
        },
        "imbalance": {"undersample_ratio": 0.2, "smote_ratio": 0.2, "smote_k_neighbors": 3},
        "evaluation": {**base.evaluation.model_dump(), "alert_rates": [0.0025, 0.01, 0.05]},
        "explain": {"global_sample_rows": 300, "examples_per_kind": 2},
        "anomaly": {"n_estimators": 50, "max_samples": 128, "fit_sample_rows": 2000},
        "mlflow": {
            "enabled": False,
            "tracking_uri": "sqlite:///unused.db",
            "experiment": "unused",
            "artifact_root": "unused",
        },
        "policy_out_dir": str(tmp / "policies"),
    }
    data.update(overrides)
    return ExperimentConfig.model_validate(data)


@pytest.fixture(scope="module")
def weak_baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A deliberately weak incumbent so that a tuned candidate wins selection."""
    out = tmp_path_factory.mktemp("baseline") / "model"
    weak = small_train_config()
    models = {
        **weak.models,
        "xgboost": {
            "n_estimators": 2,
            "learning_rate": 0.05,
            "max_depth": 1,
            "subsample": 1.0,
            "colsample_bytree": 1.0,
            "min_child_weight": 1.0,
            "reg_lambda": 1.0,
        },
    }
    cfg = weak.model_copy(update={"models": models})
    run_training(cfg, events(), out, {"dataset_version": "fixture"}, log=lambda *_: None)
    return out


class Spy:
    """Wraps ``resample`` and records which rows each call received."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, frozenset[int]]] = []

    def __call__(
        self, strategy: Any, features: pd.DataFrame, y: Any, cfg: Any, seed: int
    ) -> Resampled:
        self.calls.append((strategy, frozenset(int(i) for i in features.index)))
        return resample(strategy, features, y, cfg, seed)


@pytest.fixture(scope="module")
def run_a(
    tmp_path_factory: pytest.TempPathFactory, weak_baseline: Path
) -> tuple[dict[str, Any], RecordingTracker, Spy, ExperimentConfig, Path]:
    tmp = tmp_path_factory.mktemp("run_a")
    cfg = config(tmp, weak_baseline)
    tracker, spy = RecordingTracker(), Spy()
    mp = pytest.MonkeyPatch()
    mp.setenv("FRAUD_TOKENIZATION_KEY", SECRET)
    try:
        report = run_experiment(
            cfg,
            events(),
            tmp / "artifacts",
            {"dataset_version": "fixture"},
            tracker,
            log=lambda *_: None,
            resample_fn=spy,
        )
    finally:
        mp.undo()
    return report, tracker, spy, cfg, tmp


def _rerun(tmp: Path, baseline: Path, ev: pd.DataFrame, **overrides: Any) -> dict[str, Any]:
    cfg = config(tmp, baseline, **overrides)
    return run_experiment(
        cfg,
        ev,
        tmp / "artifacts",
        {"dataset_version": "fixture"},
        RecordingTracker(),
        log=lambda *_: None,
    )


def _training_decisions(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "trials": [(t["number"], t["state"], t["value"]) for t in r["tuning"]["trials"]],
        "best_params": r["tuning"]["best_params"],
        "cv": {s: v["mean_average_precision"] for s, v in r["imbalance"]["cv"].items()},
        "validation_ap": {k: v["average_precision"] for k, v in r["validation"].items()},
        "selected": r["selection"]["selected"],
        "threshold": r["selection"]["decision_threshold"],
        "artifact_sha256": r["selection"]["artifact_sha256"],
    }


# ---------------------------------------------------------------- temporal leakage


def test_folds_are_chronological_embargoed_and_inside_training_window() -> None:
    cfg = small_train_config()
    ev = events().sort_values(["event_time", "event_id"]).reset_index(drop=True)
    t = ev["event_time"]
    bounds = [pd.Timestamp(b) for b in ("2020-01-25", "2020-02-05", "2020-02-20")]
    bounds = [b.tz_localize("UTC").to_pydatetime() for b in bounds]
    folds = chronological_folds(t, cfg.splits.warmup_end, cfg.splits.train_end, bounds, 1)
    assert len(folds) == 2
    for f in folds:
        assert t.iloc[f.train_idx].max() < f.val_start - pd.Timedelta(days=1) + pd.Timedelta(0)
        assert t.iloc[f.train_idx].min() >= cfg.splits.warmup_end
        assert t.iloc[f.val_idx].min() >= f.val_start
        assert t.iloc[f.val_idx].max() < f.val_end <= cfg.splits.train_end
        assert not set(f.train_idx) & set(f.val_idx)
    with pytest.raises(FoldError, match="inside the training window"):
        chronological_folds(
            t,
            cfg.splits.warmup_end,
            cfg.splits.train_end,
            [*bounds[:-1], cfg.splits.validation_end],
            1,
        )
    with pytest.raises(FoldError, match="increasing"):
        chronological_folds(t, cfg.splits.warmup_end, cfg.splits.train_end, bounds[::-1], 1)


def test_tuning_ignores_validation_and_test_rows_entirely() -> None:
    """Changing labels *and* behaviour after the training window must not change any trial."""
    cfg = small_train_config()
    space = {
        "n_estimators": ParamSpec(type="int", low=20, high=40, step=20),
        "max_depth": ParamSpec(type="int", low=2, high=3),
    }

    def trial_values(ev: pd.DataFrame) -> list[float | None]:
        ev = ev.sort_values(["event_time", "event_id"]).reset_index(drop=True)
        feats = compute_offline_features(ev)
        y = ev["is_fraud"].to_numpy(np.int64)
        train_pos = np.flatnonzero(split_masks(ev, cfg.splits)["train"])
        local = chronological_folds(
            ev["event_time"].iloc[train_pos].reset_index(drop=True),
            cfg.splits.warmup_end,
            cfg.splits.train_end,
            [pd.Timestamp(b, tz="UTC").to_pydatetime() for b in ("2020-01-25", "2020-02-20")],
            1,
        )
        from dataclasses import replace

        folds = [
            replace(f, train_idx=train_pos[f.train_idx], val_idx=train_pos[f.val_idx])
            for f in local
        ]
        study = tune(folds, feats, y, space, 3, 0, 0, build_matrix, ResampleConfig())
        return [t.value for t in study.trials]

    ev = events()
    after = ev["event_time"] >= small_train_config().splits.train_end
    perturbed = ev.copy()
    perturbed.loc[after, "is_fraud"] = 1 - perturbed.loc[after, "is_fraud"]
    perturbed.loc[after, "amount"] = perturbed.loc[after, "amount"] * 50
    assert trial_values(ev) == trial_values(perturbed)


def test_resampling_only_ever_receives_training_rows(run_a: Any) -> None:
    report, _, spy, cfg, _ = run_a
    ev = events().sort_values(["event_time", "event_id"]).reset_index(drop=True)
    masks = split_masks(ev, cfg.splits)
    train_rows = frozenset(np.flatnonzero(masks["train"]).tolist())
    fold_train_sets = []
    t = ev["event_time"]
    for fold in report["folds"]:
        start, end = (pd.Timestamp(x) for x in fold["train"])
        fold_train_sets.append(
            frozenset(np.flatnonzero(((t >= start) & (t < end)).to_numpy()).tolist())
        )
    assert spy.calls, "resampling was never called"
    strategies_seen = {s for s, _ in spy.calls}
    assert strategies_seen == {"none", "class_weight", "undersample", "smotenc"}
    for _, rows in spy.calls:
        assert rows <= train_rows  # never validation or test rows
        assert rows == train_rows or rows in fold_train_sets  # exactly a fold-train or the window
    # Evaluation always used the untouched fold-validation rows (no synthetic rows scored).
    for strategy, res in report["imbalance"]["cv"].items():
        for fold, m in zip(report["folds"], res["folds"], strict=True):
            assert m["validation_rows"] == fold["validation_rows"], strategy


# ---------------------------------------------------------------- resampling validity


def test_smotenc_rows_respect_feature_definitions() -> None:
    feats = compute_offline_features(events())
    y = events()["is_fraud"].to_numpy(np.int64)
    rs = resample("smotenc", feats, y, ResampleConfig(smote_ratio=0.2, smote_k_neighbors=3), 7)
    n = len(y)
    assert rs.synthetic_rows > 0 and int(rs.y[n:].min()) == 1
    original = feats[[*NUMERIC_FEATURES, "category", "channel"]].reset_index(drop=True)
    pd.testing.assert_frame_equal(rs.features.iloc[:n].reset_index(drop=True), original)

    syn = rs.features.iloc[n:]
    x = build_matrix(syn)  # raises on any invalid category/channel value
    cat_cols = [i for i, c in enumerate(MODEL_COLUMNS) if c.startswith("category=")]
    ch_cols = [i for i, c in enumerate(MODEL_COLUMNS) if c.startswith("channel=")]
    assert np.all(x[:, cat_cols].sum(axis=1) == 1) and np.all(x[:, ch_cols].sum(axis=1) == 1)
    assert set(syn["category"]) <= set(MERCHANT_CATEGORIES) and set(syn["channel"]) <= set(CHANNELS)
    assert (syn["channel"] == syn["category"].map(derive_channel)).all()
    hour = syn["hour_of_day"]
    assert hour.between(0, 23).all() and (hour == hour.round()).all()
    assert np.allclose(syn["hour_sin"], np.sin(2 * np.pi * hour / 24))
    counts = syn[
        ["cust_txn_count_1h", "cust_txn_count_24h", "cust_txn_count_7d", "cust_txn_count_30d"]
    ].to_numpy()
    assert (counts >= 0).all() and (counts == np.round(counts)).all()
    assert (np.diff(counts, axis=1) >= 0).all()  # 1h <= 24h <= 7d <= 30d
    assert (syn["cust_amt_mean_30d"].isna() == (syn["cust_txn_count_30d"] < 1)).all()
    assert (syn["cust_amt_std_30d"].isna() == (syn["cust_txn_count_30d"] < 2)).all()
    assert (syn.loc[syn["cust_txn_count_24h"] == 0, "cust_amt_sum_24h"] == 0).all()


def test_undersampling_keeps_every_positive_and_class_weight_only_sets_weight() -> None:
    feats = compute_offline_features(events())
    y = events()["is_fraud"].to_numpy(np.int64)
    us = resample("undersample", feats, y, ResampleConfig(undersample_ratio=0.2), 0)
    assert int(us.y.sum()) == int(y.sum())
    assert math.isclose(int(us.y.sum()) / int((us.y == 0).sum()), 0.2, rel_tol=0.02)
    cw = resample("class_weight", feats, y, ResampleConfig(), 0)
    assert len(cw.y) == len(y)
    assert cw.xgb_params == {"scale_pos_weight": float((y == 0).sum() / y.sum())}


# ---------------------------------------------------------------- test-label influence


def test_test_labels_do_not_influence_any_training_decision(
    run_a: Any, tmp_path: Path, weak_baseline: Path
) -> None:
    report, _, _, cfg, _ = run_a
    ev = events()
    test = ev["event_time"] >= cfg.splits.validation_end
    flipped = ev.assign(is_fraud=np.where(test, 1 - ev["is_fraud"], ev["is_fraud"]))
    other = _rerun(tmp_path, weak_baseline, flipped)
    assert _training_decisions(other) == _training_decisions(report)
    assert (
        other["test"]["selected"]["average_precision"]
        != report["test"]["selected"]["average_precision"]
    )


def test_experiment_is_reproducible_and_anomaly_does_not_affect_the_model(
    run_a: Any, tmp_path: Path, weak_baseline: Path
) -> None:
    report = run_a[0]
    without_anomaly = _rerun(
        tmp_path,
        weak_baseline,
        events(),
        anomaly={"enabled": False, "n_estimators": 50, "max_samples": 128, "fit_sample_rows": 2000},
    )
    assert _training_decisions(without_anomaly) == _training_decisions(report)
    assert without_anomaly["anomaly"] == {"enabled": False}


# ---------------------------------------------------------------- new version + policy


def test_new_model_gets_new_version_bound_policy_and_is_not_promoted(
    run_a: Any, weak_baseline: Path
) -> None:
    report, _, _, cfg, tmp = run_a
    sel = report["selection"]
    assert sel["new_model"] is True and sel["promoted"] is False
    assert sel["model_version"] == "0.1.0" and sel["selected"].startswith("tuned:")
    model = load_artifact(Path(report["artifact_dir"]))  # hash-verified JSON load
    assert model.version == "0.1.0"
    assert model.manifest["feature_columns"] == list(MODEL_COLUMNS)
    assert model.manifest["lineage"]["parent_model"] == "test-model@0.0.1"
    policy = DecisionPolicy.load(Path(sel["candidate_policy"]))
    policy.check_model("test-model", "0.1.0")
    with pytest.raises(ValueError, match="bound to"):
        policy.check_model("test-model", "0.0.1")
    assert policy.thresholds.review_at == pytest.approx(sel["decision_threshold"], abs=1e-6)
    baseline = load_artifact(weak_baseline)
    assert baseline.sha256 == load_artifact(weak_baseline).sha256  # incumbent untouched
    with pytest.raises(ExperimentError, match="refusing to overwrite"):
        run_experiment(
            cfg,
            events(),
            tmp / "artifacts",
            {"dataset_version": "fixture"},
            RecordingTracker(),
            log=lambda *_: None,
        )


def test_incumbent_is_kept_when_it_wins(
    tmp_path: Path, weak_baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When selection keeps the incumbent: no new artifact, no candidate policy, and the
    incumbent's own validation threshold is reported."""
    monkeypatch.setattr(
        experiment_module,
        "select_model",
        lambda validation, tol, candidates: next(
            k for k in validation if k.startswith("baseline:")
        ),
    )
    report = _rerun(tmp_path, weak_baseline, events())
    sel = report["selection"]
    assert sel["new_model"] is False and sel["model_version"] == "0.0.1"
    assert report["artifact_dir"] is None and sel["candidate_policy"] is None
    assert not (tmp_path / "artifacts").exists() and not (tmp_path / "policies").exists()
    baseline = load_artifact(weak_baseline)
    assert sel["decision_threshold"] == baseline.manifest["decision_threshold"]["value"]
    assert sel["artifact_sha256"] == baseline.sha256


# ---------------------------------------------------------------- explanations


def test_shap_contributions_reconstruct_log_odds_and_probability(
    weak_baseline: Path, run_a: Any
) -> None:
    model = load_artifact(Path(run_a[0]["artifact_dir"]))
    x = build_matrix(compute_offline_features(events()).iloc[:500])
    c = tree_shap(model.booster, x)
    assert c.reconstruction_error < 1e-4
    assert np.allclose(1 / (1 + np.exp(-c.margin)), model.predict_proba(x), atol=1e-6)
    assert np.allclose(c.grouped().sum(axis=1), c.values.sum(axis=1), atol=1e-9)
    report = run_a[0]["explanations"]
    assert report["units"].startswith("log-odds")
    assert report["max_reconstruction_error"] < 1e-3


def test_broken_contributions_are_detected(run_a: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    booster = load_artifact(Path(run_a[0]["artifact_dir"])).booster
    real_predict = booster.predict

    def shifted(dm: xgb.DMatrix, **kw: Any) -> Any:
        out = real_predict(dm, **kw)
        return out + 0.5 if kw.get("pred_contribs") else out

    monkeypatch.setattr(booster, "predict", shifted)
    x = build_matrix(compute_offline_features(events()).iloc[:50])
    with pytest.raises(ExplanationError, match="reconstruct"):
        tree_shap(booster, x)


def test_explanations_refuse_identifiers(run_a: Any) -> None:
    booster = load_artifact(Path(run_a[0]["artifact_dir"])).booster
    feats = compute_offline_features(events()).iloc[:3]
    c = tree_shap(booster, build_matrix(feats))
    with pytest.raises(ExplanationError, match="non-feature"):
        explain_rows(c, feats.assign(customer_id="cus_0000000000000001"), ["a", "b", "c"])


# ---------------------------------------------------------------- anomaly separation


def test_anomaly_score_cannot_reach_the_decision_policy_or_model() -> None:
    feats = compute_offline_features(events())
    det = BehavioralAnomalyDetector(n_estimators=20, max_samples=64, seed=0)
    det.fit(feats[list(ANOMALY_FEATURES)])
    scores = det.score(feats[list(ANOMALY_FEATURES)].iloc[:10])
    assert isinstance(scores, AnomalyScores) and "not a probability" in scores.units
    policy = DecisionPolicy.load(Path("ml/configs/decision_policy.yaml"))
    with pytest.raises(TypeError, match="fraud probability"):
        policy.decide(scores)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        float(scores)  # type: ignore[arg-type]
    assert not any("anomaly" in c for c in MODEL_COLUMNS)
    with pytest.raises(AnomalyInputError):
        det.fit(feats[list(ANOMALY_FEATURES)].assign(is_fraud=0))
    with pytest.raises(AnomalyInputError):
        det.score(feats[list(ANOMALY_FEATURES)].assign(risk_score=0.5))


def test_anomaly_fit_is_label_free_and_reported_separately(run_a: Any) -> None:
    import inspect

    assert list(inspect.signature(BehavioralAnomalyDetector.fit).parameters) == ["self", "features"]
    report = run_a[0]
    assert report["anomaly"]["units"].endswith("not a probability")
    assert "anomaly" not in json.dumps(report["selection"])


# ---------------------------------------------------------------- tracking hygiene


def test_tracker_records_contain_no_transaction_data_or_secrets(run_a: Any) -> None:
    _, tracker, _, _, _ = run_a
    blob = json.dumps([r for r in tracker.records], default=str)
    assert "cus_" not in blob and SECRET not in blob
    ev = events()
    assert not any(e in blob for e in ev["event_id"].head(500))
    names = {(run, kind) for run, kind, _ in tracker.records}
    assert ("tuning", "metrics") in names and ("selection", "tags") in names


@pytest.mark.parametrize(
    "value",
    ["cus_0123456789abcdef", "evt 0123456789abcdef0123456789abcdef", {"k": [SECRET]}],
)
def test_guard_rejects_sensitive_values(value: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAUD_JWT_SECRET", SECRET)
    with pytest.raises(SensitiveDataError):
        guard(value)
    guard({"sha256": "a" * 64, "commit": "b" * 40})  # hashes are allowed


def test_mlflow_tracking_end_to_end(tmp_path: Path, weak_baseline: Path) -> None:
    from mlflow.tracking import MlflowClient

    from fraud_ml.tracking import MlflowTracker

    db = tmp_path / "mlflow.db"
    cfg = config(
        tmp_path,
        weak_baseline,
        mlflow={
            "enabled": True,
            "tracking_uri": f"sqlite:///{db}",
            "experiment": "phase3-test",
            "artifact_root": str(tmp_path / "mlart"),
        },
    )
    tracker = MlflowTracker(
        cfg.mlflow.tracking_uri, cfg.mlflow.experiment, cfg.mlflow.artifact_root
    )
    report = run_experiment(
        cfg,
        events(),
        tmp_path / "artifacts",
        {"dataset_version": "fixture"},
        tracker,
        log=lambda *_: None,
    )
    client = MlflowClient(tracking_uri=cfg.mlflow.tracking_uri)
    exp = client.get_experiment_by_name("phase3-test")
    runs = client.search_runs([exp.experiment_id])
    names = {r.info.run_name for r in runs}
    assert {"phase3:test-model@0.1.0", "tuning", "cv:smotenc", "selection", "test"} <= names
    parent = next(r for r in runs if r.info.run_name == "phase3:test-model@0.1.0")
    assert parent.data.tags["dataset_version"] == "fixture"
    assert parent.data.tags["baseline_sha256"] == load_artifact(weak_baseline).sha256
    selection = next(r for r in runs if r.info.run_name == "selection")
    assert selection.data.tags["artifact_sha256"] == report["selection"]["artifact_sha256"]

    dump = "\n".join(
        " ".join(map(str, row))
        for table in ("params", "tags", "metrics", "runs")
        for row in sqlite3.connect(db).execute(f"SELECT * FROM {table}")  # noqa: S608
    )
    for path in (tmp_path / "mlart").rglob("*"):
        if path.is_file():
            dump += path.read_text(errors="ignore")
    assert "cus_" not in dump
    assert not any(e in dump for e in events()["event_id"])


# ---------------------------------------------------------------- promotion gate


def _canonical(tmp: Path) -> Path:
    ev = events().assign(event_time=lambda d: d["event_time"].astype("datetime64[us, UTC]"))
    path = tmp / "card_transactions.parquet"
    ev.to_parquet(path, index=False)
    (tmp / MANIFEST_FILE).write_text(
        json.dumps(
            {
                "dataset_version": "fixture",
                "canonical_schema_version": "1",
                "output": {"path": path.name, "sha256": sha256_file(path)},
            }
        )
    )
    return path


def test_promotion_gates_pass_and_apply_is_reversible(run_a: Any, tmp_path: Path) -> None:
    report = run_a[0]
    artifact = Path(report["artifact_dir"])
    policy = Path(report["selection"]["candidate_policy"])
    data = _canonical(tmp_path)
    gates = check_promotion(artifact, policy, data, sample=150)
    assert [g.name for g in gates] == ["artifact", "policy", "threshold", "parity", "decisions"]
    assert all(g.passed for g in gates), gates

    # Apply against a sandbox copy of the serving layout.
    root = tmp_path / "serving"
    (root / "artifacts").mkdir(parents=True)
    import shutil

    shutil.copytree(artifact, root / "artifacts" / artifact.name)
    old = root / "artifacts" / "test-model-0.0.1"
    old.mkdir()
    (root / "artifacts" / "current").symlink_to(old.name)
    active = root / "decision_policy.yaml"
    active.write_text(
        Path("ml/configs/decision_policy.yaml")
        .read_text()
        .replace("card-fraud-xgb", "test-model")
        .replace("1.0.0", "0.0.1")
    )
    before = hashlib.sha256(active.read_bytes()).hexdigest()
    msg = apply_promotion(
        root / "artifacts" / artifact.name,
        policy,
        root / "artifacts" / "current",
        active,
        root / "archive",
    )
    assert (root / "artifacts" / "current").resolve().name == artifact.name
    assert DecisionPolicy.load(active).model.version == "0.1.0"
    backup = root / "archive" / "test-model-0.0.1.yaml"
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == before
    assert "Rollback" in msg


def test_promotion_gates_fail_on_mismatch(run_a: Any, tmp_path: Path, weak_baseline: Path) -> None:
    report = run_a[0]
    data = _canonical(tmp_path)
    # Policy bound to the candidate, artifact is the incumbent -> policy gate fails.
    gates = check_promotion(weak_baseline, Path(report["selection"]["candidate_policy"]), data, 20)
    assert not gates[-1].passed and gates[-1].name == "policy"
    # Tampered artifact -> artifact gate fails.
    import shutil

    bad = tmp_path / "bad"
    shutil.copytree(Path(report["artifact_dir"]), bad)
    with (bad / "model.json").open("a") as fh:
        fh.write(" ")
    gates = check_promotion(bad, Path(report["selection"]["candidate_policy"]), data, 20)
    assert [(g.name, g.passed) for g in gates] == [("artifact", False)]
