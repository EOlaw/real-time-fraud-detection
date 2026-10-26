"""Vectorized point-in-time feature computation for training and backtesting.

Implements the rule in ``fraud_ml.features.spec`` with per-entity binary search over sorted event
times: ``searchsorted(times, t, "left")`` counts events strictly before ``t`` and
``searchsorted(times, t - w, "left")`` finds the first event inside ``[t - w, t)``. Window sums use
prefix sums, so the whole dataset is processed in O(n log n) without row-level Python loops.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

from fraud_ml.features.spec import (
    WINDOWS_US,
    derived_amount_features,
    time_features,
)

FloatArray = npt.NDArray[np.float64]


def _to_us(times: pd.Series) -> npt.NDArray[np.int64]:
    utc = times.dt.tz_convert("UTC").astype("datetime64[us, UTC]")
    return np.asarray(utc.astype("int64").to_numpy(), dtype=np.int64)


def _group_window_stats(
    t_us: npt.NDArray[np.int64],
    amount: FloatArray,
    group_codes: npt.NDArray[np.int64],
    windows: dict[str, int],
    with_moments: bool,
) -> dict[str, FloatArray]:
    """Per-group trailing window counts (and optionally sum/mean/std) for each row.

    Rows must already be sorted by (group, time). Returns arrays aligned with the input rows.
    """
    n = len(t_us)
    out: dict[str, FloatArray] = {}
    for name in windows:
        out[f"count_{name}"] = np.zeros(n)
        if with_moments:
            out[f"sum_{name}"] = np.zeros(n)
            out[f"mean_{name}"] = np.full(n, np.nan)
            out[f"std_{name}"] = np.full(n, np.nan)
    out["prev_gap_s"] = np.full(n, np.nan)

    boundaries = np.flatnonzero(np.diff(group_codes)) + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [n]])
    for start, end in zip(starts, ends, strict=True):
        times = t_us[start:end]
        hi = np.searchsorted(times, times, side="left")  # events strictly before t
        has_prev = hi > 0
        gap = np.full(end - start, np.nan)
        gap[has_prev] = (times[has_prev] - times[hi[has_prev] - 1]) / 1e6
        out["prev_gap_s"][start:end] = gap
        if with_moments:
            amt = amount[start:end]
            # Shift by the group mean before squaring to limit floating-point cancellation.
            centered = amt - amt.mean()
            csum = np.concatenate([[0.0], np.cumsum(amt)])
            ccen = np.concatenate([[0.0], np.cumsum(centered)])
            csq = np.concatenate([[0.0], np.cumsum(centered**2)])
        for name, width in windows.items():
            lo = np.searchsorted(times, times - width, side="left")
            cnt = (hi - lo).astype(np.float64)
            out[f"count_{name}"][start:end] = cnt
            if with_moments:
                s = csum[hi] - csum[lo]
                sc = ccen[hi] - ccen[lo]
                sq = csq[hi] - csq[lo]
                with np.errstate(divide="ignore", invalid="ignore"):
                    mean = np.where(cnt > 0, s / cnt, np.nan)
                    var = np.where(cnt > 1, (sq - sc**2 / cnt) / (cnt - 1), np.nan)
                out[f"sum_{name}"][start:end] = s
                out[f"mean_{name}"][start:end] = mean
                out[f"std_{name}"][start:end] = np.sqrt(np.clip(var, 0.0, None))
    return out


def compute_offline_features(events: pd.DataFrame) -> pd.DataFrame:
    """Compute features for every event using only that customer's strictly-earlier events.

    ``events`` needs columns: event_id, event_time (tz-aware), customer_id, category, channel,
    amount. Returns a frame indexed like ``events`` with event_id, all numeric features, and the
    categorical columns.
    """
    df = events[["event_id", "event_time", "customer_id", "category", "channel", "amount"]].copy()
    df["_row"] = np.arange(len(df))
    df["_t"] = _to_us(df["event_time"])
    amount = df["amount"].to_numpy(dtype=np.float64)

    # Customer-level windows.
    by_cust = df.sort_values(["customer_id", "_t"], kind="mergesort")
    order = by_cust["_row"].to_numpy()
    codes = pd.factorize(by_cust["customer_id"])[0].astype(np.int64)
    cust = _group_window_stats(
        by_cust["_t"].to_numpy(),
        amount[order],
        codes,
        {
            "1h": WINDOWS_US["1h"],
            "24h": WINDOWS_US["24h"],
            "7d": WINDOWS_US["7d"],
            "30d": WINDOWS_US["30d"],
        },
        with_moments=True,
    )
    # Customer x category windows.
    by_cc = df.sort_values(["customer_id", "category", "_t"], kind="mergesort")
    order_cc = by_cc["_row"].to_numpy()
    codes_cc = pd.factorize(by_cc["customer_id"].astype(str) + "|" + by_cc["category"].astype(str))[
        0
    ].astype(np.int64)
    cc = _group_window_stats(
        by_cc["_t"].to_numpy(),
        amount[order_cc],
        codes_cc,
        {"30d": WINDOWS_US["30d"]},
        with_moments=False,
    )

    def unsort(values: FloatArray, idx: npt.NDArray[np.int64]) -> FloatArray:
        restored = np.empty_like(values)
        restored[idx] = values
        return restored

    mean_30d = unsort(cust["mean_30d"], order)
    std_30d = unsort(cust["std_30d"], order)
    ts = df["event_time"].dt.tz_convert("UTC")
    features: dict[str, FloatArray] = {
        **time_features(ts.dt.hour.to_numpy(np.int64), ts.dt.day_of_week.to_numpy(np.int64)),
        "cust_txn_count_1h": unsort(cust["count_1h"], order),
        "cust_txn_count_24h": unsort(cust["count_24h"], order),
        "cust_txn_count_7d": unsort(cust["count_7d"], order),
        "cust_txn_count_30d": unsort(cust["count_30d"], order),
        "cust_amt_sum_24h": unsort(cust["sum_24h"], order),
        "cust_amt_mean_30d": mean_30d,
        "cust_amt_std_30d": std_30d,
        "seconds_since_prev_txn": unsort(cust["prev_gap_s"], order),
        "cust_same_category_count_30d": unsort(cc["count_30d"], order_cc),
        **derived_amount_features(amount, mean_30d, std_30d),
    }
    result = pd.DataFrame(features, index=events.index)
    result.insert(0, "event_id", events["event_id"].to_numpy())
    result["category"] = events["category"].to_numpy()
    result["channel"] = events["channel"].to_numpy()
    return result
