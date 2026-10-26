"""Single-event feature computation for real-time scoring.

Deliberately written as direct array filters (not prefix sums) so it is an independent
implementation of the same definition as ``offline.py``; ``tests/unit/test_feature_parity.py``
checks that the two agree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from fraud_ml.features.spec import (
    MAX_LOOKBACK_US,
    WINDOWS_US,
    derived_amount_features,
    time_features,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)


def to_epoch_us(ts: datetime) -> int:
    if ts.tzinfo is None:
        raise ValueError("event_time must be timezone-aware")
    return (ts - _EPOCH) // _ONE_US


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    event_time_us: int
    amount: float
    category: str


def compute_online_features(
    event_time: datetime,
    amount: float,
    category: str,
    channel: str,
    history: Sequence[HistoryEvent],
) -> dict[str, float | str]:
    """Features for one event. ``history`` may contain any events; only those of the same customer
    within ``[t - 30d, t)`` are used, so callers can over-fetch safely but must not under-fetch."""
    t = to_epoch_us(event_time)
    times = np.fromiter((h.event_time_us for h in history), dtype=np.int64, count=len(history))
    amounts = np.fromiter((h.amount for h in history), dtype=np.float64, count=len(history))
    cats = np.array([h.category for h in history], dtype=object)

    before = times < t
    times, amounts, cats = times[before], amounts[before], cats[before]
    if len(times) and times.min() < t - MAX_LOOKBACK_US:
        keep = times >= t - MAX_LOOKBACK_US
        times, amounts, cats = times[keep], amounts[keep], cats[keep]

    def in_window(name: str) -> np.ndarray:
        return times >= t - WINDOWS_US[name]

    w24, w30 = in_window("24h"), in_window("30d")
    a30 = amounts[w30]
    mean_30d = float(a30.mean()) if len(a30) else float("nan")
    std_30d = float(a30.std(ddof=1)) if len(a30) > 1 else float("nan")

    ts_utc = event_time.astimezone(UTC)
    tf = time_features(np.array([ts_utc.hour]), np.array([ts_utc.weekday()]))
    amt_arr = np.array([float(amount)])
    derived = derived_amount_features(amt_arr, np.array([mean_30d]), np.array([std_30d]))

    features: dict[str, float | str] = {k: float(v[0]) for k, v in {**tf, **derived}.items()}
    features.update(
        {
            "cust_txn_count_1h": float(in_window("1h").sum()),
            "cust_txn_count_24h": float(w24.sum()),
            "cust_txn_count_7d": float(in_window("7d").sum()),
            "cust_txn_count_30d": float(w30.sum()),
            "cust_amt_sum_24h": float(amounts[w24].sum()),
            "cust_amt_mean_30d": mean_30d,
            "cust_amt_std_30d": std_30d,
            "seconds_since_prev_txn": (
                float((t - times.max()) / 1e6) if len(times) else float("nan")
            ),
            "cust_same_category_count_30d": float((cats[w30] == category).sum()),
            "category": category,
            "channel": channel,
        }
    )
    return features
