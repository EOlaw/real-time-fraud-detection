"""Experiment tracking (MLflow) with a guard against logging transaction data or secrets.

Only configuration, versions, hashes and aggregate metrics are logged. Every string that reaches
the tracker is checked: customer tokens (``cus_…``), 32-hex event ids and the values of secret
environment variables raise ``SensitiveDataError`` instead of being written. Hashes (64 hex) are
allowed because they are not transaction identifiers.

Local setup: ``sqlite:///ml/mlruns/mlflow.db`` with artifacts under ``ml/mlruns/artifacts``
(git-ignored). Browse with ``make mlflow-ui``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

SECRET_ENV_VARS: Final = (
    "FRAUD_TOKENIZATION_KEY",
    "FRAUD_JWT_SECRET",
    "POSTGRES_PASSWORD",
    "FRAUD_DATABASE_URL",
    "FRAUD_NEW_USER_PASSWORD",
    "FRAUD_REPLAY_PASSWORD",
)
_CUSTOMER_TOKEN = re.compile(r"cus_[0-9a-f]{16}")
_EVENT_ID = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")


class SensitiveDataError(ValueError):
    pass


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _strings(v)
    elif obj is not None:
        yield str(obj)


def guard(obj: Any) -> None:
    secrets = [v for k in SECRET_ENV_VARS if len(v := os.environ.get(k, "")) >= 8]
    for s in _strings(obj):
        if _CUSTOMER_TOKEN.search(s):
            raise SensitiveDataError("refusing to log a customer token")
        if _EVENT_ID.search(s):
            raise SensitiveDataError("refusing to log a transaction/event id")
        if any(secret in s for secret in secrets):
            raise SensitiveDataError("refusing to log a secret value")


def flatten(d: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(flatten(v, f"{key}."))
        else:
            out[key] = json.dumps(v) if isinstance(v, (list, tuple)) else v
    return out


class Tracker(Protocol):
    def run(self, name: str, nested: bool = False) -> Any: ...
    def params(self, values: Mapping[str, Any]) -> None: ...
    def metrics(self, values: Mapping[str, float], step: int | None = None) -> None: ...
    def tags(self, values: Mapping[str, str]) -> None: ...
    def artifact_dict(self, obj: Mapping[str, Any], name: str) -> None: ...
    def artifact_file(self, path: Path) -> None: ...


@dataclass
class RecordingTracker:
    """In-memory tracker (tests, dry runs). Applies the same guard as the MLflow tracker."""

    records: list[tuple[str, str, Any]] = field(default_factory=list)
    _stack: list[str] = field(default_factory=list)

    @contextmanager
    def run(self, name: str, nested: bool = False) -> Iterator[None]:
        guard(name)
        self._stack.append(name)
        try:
            yield
        finally:
            self._stack.pop()

    def _rec(self, kind: str, value: Any) -> None:
        guard(value)
        self.records.append((self._stack[-1] if self._stack else "", kind, value))

    def params(self, values: Mapping[str, Any]) -> None:
        self._rec("params", dict(values))

    def metrics(self, values: Mapping[str, float], step: int | None = None) -> None:
        self._rec("metrics", {**values, "_step": step})

    def tags(self, values: Mapping[str, str]) -> None:
        self._rec("tags", dict(values))

    def artifact_dict(self, obj: Mapping[str, Any], name: str) -> None:
        self._rec("artifact", {"name": name, "content": obj})

    def artifact_file(self, path: Path) -> None:
        self._rec("artifact_file", {"name": path.name, "content": path.read_text()})


class MlflowTracker:
    def __init__(self, tracking_uri: str, experiment: str, artifact_root: Path) -> None:
        os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
        import mlflow

        self._mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri)
        if mlflow.get_experiment_by_name(experiment) is None:
            artifact_root.mkdir(parents=True, exist_ok=True)
            mlflow.create_experiment(experiment, artifact_location=artifact_root.resolve().as_uri())
        mlflow.set_experiment(experiment)

    @contextmanager
    def run(self, name: str, nested: bool = False) -> Iterator[None]:
        guard(name)
        with self._mlflow.start_run(run_name=name, nested=nested):
            yield

    def params(self, values: Mapping[str, Any]) -> None:
        guard(values)
        self._mlflow.log_params({k: str(v)[:6000] for k, v in values.items()})

    def metrics(self, values: Mapping[str, float], step: int | None = None) -> None:
        guard(values)
        finite = {k: float(v) for k, v in values.items() if v is not None and v == v}
        self._mlflow.log_metrics(finite, step=step)

    def tags(self, values: Mapping[str, str]) -> None:
        guard(values)
        self._mlflow.set_tags(dict(values))

    def artifact_dict(self, obj: Mapping[str, Any], name: str) -> None:
        guard(obj)
        self._mlflow.log_dict(dict(obj), name)

    def artifact_file(self, path: Path) -> None:
        guard(path.read_text())
        self._mlflow.log_artifact(str(path))
