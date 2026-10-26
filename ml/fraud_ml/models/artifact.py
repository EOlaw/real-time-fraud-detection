"""Versioned model artifacts without pickle.

An artifact directory contains:
  * ``model.json``    - XGBoost native JSON model (data only; loading it cannot execute code)
  * ``manifest.json`` - feature contract, dataset version, thresholds, metrics, model SHA-256

Loading verifies the model file hash and the feature contract before the model is used. We never
``pickle``/``joblib`` load serving artifacts: those formats execute arbitrary code on load.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import xgboost as xgb

from fraud_ml.features.spec import FEATURE_SPEC_VERSION, MODEL_COLUMNS

MODEL_FILE = "model.json"
MANIFEST_FILE = "manifest.json"


class ArtifactError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_artifact(booster: xgb.Booster, out_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / MODEL_FILE
    booster.save_model(model_path)
    manifest = {
        **manifest,
        "model_format": "xgboost-json",
        "model_file": MODEL_FILE,
        "model_sha256": _sha256(model_path),
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "feature_columns": list(MODEL_COLUMNS),
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, default=float))
    return manifest


@dataclass(frozen=True)
class LoadedModel:
    booster: xgb.Booster
    manifest: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.manifest["model_name"])

    @property
    def version(self) -> str:
        return str(self.manifest["model_version"])

    @property
    def sha256(self) -> str:
        return str(self.manifest["model_sha256"])

    def predict_proba(self, x: npt.NDArray[np.float32]) -> npt.NDArray[np.float64]:
        if x.ndim != 2 or x.shape[1] != len(MODEL_COLUMNS):
            raise ArtifactError(f"Expected (n, {len(MODEL_COLUMNS)}) matrix, got {x.shape}")
        dm = xgb.DMatrix(x, feature_names=list(MODEL_COLUMNS), missing=np.nan)
        p = self.booster.predict(dm).astype(np.float64)
        if not np.all(np.isfinite(p)) or p.min() < 0 or p.max() > 1:
            raise ArtifactError("Model produced non-probability outputs")
        return p


def load_artifact(directory: Path, expected_sha256: str | None = None) -> LoadedModel:
    directory = directory.resolve()
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ArtifactError(f"No {MANIFEST_FILE} in {directory}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("model_format") != "xgboost-json":
        raise ArtifactError(f"Unsupported model format: {manifest.get('model_format')!r}")
    model_path = (directory / manifest["model_file"]).resolve()
    if model_path.parent != directory:
        raise ArtifactError("Model file must live inside the artifact directory")
    actual = _sha256(model_path)
    if actual != manifest["model_sha256"]:
        raise ArtifactError("Model file hash does not match manifest")
    if expected_sha256 is not None and actual != expected_sha256:
        raise ArtifactError("Model file hash does not match the registered version")
    if (
        manifest.get("feature_spec_version") != FEATURE_SPEC_VERSION
        or tuple(manifest.get("feature_columns", ())) != MODEL_COLUMNS
    ):
        raise ArtifactError("Artifact feature contract does not match this code version")
    booster = xgb.Booster()
    booster.load_model(model_path)
    return LoadedModel(booster=booster, manifest=manifest)
