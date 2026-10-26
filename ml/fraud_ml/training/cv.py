"""Chronological (expanding-window) cross-validation inside the training window.

Fold *k* trains on ``[train_start, boundary_k - embargo)`` and validates on
``[boundary_k, boundary_{k+1})``. Boundaries are explicit timestamps from the config, so folds are
identical on every run. The last boundary must not exceed the training window end, so tuning never
sees the model-selection validation window or the held-out test window.

The embargo drops training rows just before each validation block. Fraud arrives in short
per-customer bursts; without a gap, labels from the start of a burst would sit in fold-train while
the rest of the burst is in fold-validation, which flatters CV scores relative to production
(where labels arrive late).
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import numpy.typing as npt
import pandas as pd


@dataclass(frozen=True)
class Fold:
    index: int
    train_idx: npt.NDArray[np.int64]
    val_idx: npt.NDArray[np.int64]
    train_start: datetime
    train_end: datetime  # exclusive (validation start minus embargo)
    val_start: datetime
    val_end: datetime  # exclusive

    def describe(self) -> dict[str, object]:
        return {
            "fold": self.index,
            "train": [self.train_start.isoformat(), self.train_end.isoformat()],
            "validation": [self.val_start.isoformat(), self.val_end.isoformat()],
            "train_rows": len(self.train_idx),
            "validation_rows": len(self.val_idx),
        }


class FoldError(ValueError):
    pass


def chronological_folds(
    event_time: pd.Series,
    train_start: datetime,
    train_end: datetime,
    boundaries: Sequence[datetime],
    embargo_days: float,
) -> list[Fold]:
    """Row indices (positions in ``event_time``) for each fold. Only rows inside
    ``[train_start, train_end)`` are ever selected."""
    bounds = list(boundaries)
    if len(bounds) < 2:
        raise FoldError("need at least two boundaries (one fold)")
    if bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise FoldError("boundaries must be strictly increasing")
    if bounds[0] <= train_start or bounds[-1] > train_end:
        raise FoldError("boundaries must lie inside the training window")
    embargo = timedelta(days=embargo_days)
    folds = []
    for k, (val_start, val_end) in enumerate(itertools.pairwise(bounds)):
        fit_end = val_start - embargo
        if fit_end <= train_start:
            raise FoldError(f"fold {k} has no training data after the embargo")
        train_mask = ((event_time >= train_start) & (event_time < fit_end)).to_numpy()
        val_mask = ((event_time >= val_start) & (event_time < val_end)).to_numpy()
        folds.append(
            Fold(
                index=k,
                train_idx=np.flatnonzero(train_mask).astype(np.int64),
                val_idx=np.flatnonzero(val_mask).astype(np.int64),
                train_start=train_start,
                train_end=fit_end,
                val_start=val_start,
                val_end=val_end,
            )
        )
    return folds
