from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import MODEL_COLUMNS
from fraud_ml.models.artifact import MANIFEST_FILE, MODEL_FILE, ArtifactError, load_artifact
from fraud_ml.training.train import build_matrix, run_training, split_masks
from tests.conftest import small_train_config
from tests.fixtures import synthetic_events


def _train(out: Path, flip_test_labels: bool = False) -> dict[str, object]:
    cfg = small_train_config()
    events = synthetic_events(n=6_000, n_customers=40, seed=11)
    if flip_test_labels:
        test = split_masks(events, cfg.splits)["test"]
        events.loc[test, "is_fraud"] = 1 - events.loc[test, "is_fraud"]
    return run_training(cfg, events, out, {"dataset_version": "fixture"}, log=lambda *_: None)


def test_artifact_round_trip_and_probabilities(trained_artifact: Path) -> None:
    model = load_artifact(trained_artifact)
    events = synthetic_events(n=500, n_customers=10, seed=3)
    x = build_matrix(compute_offline_features(events))
    p = model.predict_proba(x)
    assert p.shape == (500,)
    assert np.all((p >= 0) & (p <= 1))
    assert model.manifest["feature_columns"] == list(MODEL_COLUMNS)


def test_tampered_model_file_is_rejected(trained_artifact: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(trained_artifact, copy)
    with (copy / MODEL_FILE).open("a") as fh:
        fh.write(" ")
    with pytest.raises(ArtifactError, match="hash"):
        load_artifact(copy)


def test_feature_contract_mismatch_is_rejected(trained_artifact: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(trained_artifact, copy)
    manifest = json.loads((copy / MANIFEST_FILE).read_text())
    manifest["feature_columns"] = manifest["feature_columns"][:-1]
    (copy / MANIFEST_FILE).write_text(json.dumps(manifest))
    with pytest.raises(ArtifactError, match="feature contract"):
        load_artifact(copy)


def test_model_file_outside_artifact_dir_is_rejected(
    trained_artifact: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(trained_artifact, copy)
    manifest = json.loads((copy / MANIFEST_FILE).read_text())
    manifest["model_file"] = "../../etc/passwd"
    (copy / MANIFEST_FILE).write_text(json.dumps(manifest))
    with pytest.raises(ArtifactError):
        load_artifact(copy)


def test_registered_hash_must_match(trained_artifact: Path) -> None:
    with pytest.raises(ArtifactError, match="registered"):
        load_artifact(trained_artifact, expected_sha256="0" * 64)


def test_training_is_reproducible(tmp_path: Path) -> None:
    a, b = _train(tmp_path / "a"), _train(tmp_path / "b")
    assert a["model_sha256"] == b["model_sha256"]
    assert a["decision_threshold"] == b["decision_threshold"]


def test_test_labels_do_not_influence_selection_or_threshold(tmp_path: Path) -> None:
    """Leakage guard: the held-out window must not affect any training-time decision."""
    normal = _train(tmp_path / "normal")
    flipped = _train(tmp_path / "flipped", flip_test_labels=True)
    assert normal["selected_for_serving"] == flipped["selected_for_serving"]
    assert normal["decision_threshold"] == flipped["decision_threshold"]
    assert normal["model_sha256"] == flipped["model_sha256"]

    def without_timing(report: dict[str, object]) -> dict[str, object]:
        return {
            name: {k: v for k, v in m.items() if k != "fit_seconds"}
            for name, m in report["validation"].items()
        }  # type: ignore[attr-defined]

    a, b = without_timing(normal), without_timing(flipped)
    assert a.keys() == b.keys()
    for name in a:
        # Parallel random-forest averaging is not bit-exact across runs (~1e-18 differences).
        assert a[name] == pytest.approx(b[name], rel=1e-9), name
    # ...while the test metrics themselves do change, proving the test labels were read.
    assert normal["test"]["average_precision"] != flipped["test"]["average_precision"]


def test_splits_are_disjoint_and_ordered() -> None:
    cfg = small_train_config()
    events = synthetic_events(n=6_000, n_customers=40, seed=11)
    m = split_masks(events, cfg.splits)
    t = events["event_time"]
    assert t[m["train"]].max() < t[m["validation"]].min()
    assert t[m["validation"]].max() < t[m["test"]].min()
    assert not (m["train"] & m["test"]).any()
