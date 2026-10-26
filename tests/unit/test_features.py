from __future__ import annotations

import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.online import HistoryEvent, compute_online_features, to_epoch_us
from fraud_ml.features.spec import MODEL_COLUMNS, NUMERIC_FEATURES, encode
from tests.fixtures import synthetic_events


@pytest.fixture(scope="module")
def events() -> pd.DataFrame:
    return synthetic_events()


def _online_for_row(events: pd.DataFrame, i: int) -> dict[str, float | str]:
    row = events.iloc[i]
    same = events[events["customer_id"] == row["customer_id"]]
    history = [
        HistoryEvent(to_epoch_us(r.event_time.to_pydatetime()), float(r.amount), r.category)
        for r in same.itertuples()
        if r.Index != events.index[i]
    ]
    return compute_online_features(
        row["event_time"].to_pydatetime(),
        float(row["amount"]),
        row["category"],
        row["channel"],
        history,
    )


def test_offline_online_parity(events: pd.DataFrame) -> None:
    offline = compute_offline_features(events)
    rng = np.random.default_rng(0)
    rows = list(rng.choice(len(events), size=300, replace=False)) + list(range(10, 15))
    for i in rows:
        online = _online_for_row(events, int(i))
        for name in NUMERIC_FEATURES:
            a, b = float(offline.iloc[i][name]), float(online[name])
            if math.isnan(a) or math.isnan(b):
                assert math.isnan(a) and math.isnan(b), (i, name, a, b)
            else:
                assert b == pytest.approx(a, rel=1e-9, abs=1e-6), (i, name, a, b)


def test_features_do_not_see_future_events(events: pd.DataFrame) -> None:
    """Leakage check: perturbing or adding later events must not change earlier features."""
    cutoff = events["event_time"].iloc[len(events) // 2]
    past = events[events["event_time"] < cutoff]
    baseline = compute_offline_features(past)

    future = events[events["event_time"] >= cutoff].copy()
    future["amount"] = future["amount"] * 100  # wildly different future behavior
    extra = future.copy()
    extra["event_id"] = extra["event_id"] + "x"
    combined = pd.concat([past, future, extra], ignore_index=True)
    recomputed = compute_offline_features(combined).iloc[: len(past)]

    pd.testing.assert_frame_equal(
        baseline.reset_index(drop=True), recomputed.reset_index(drop=True)
    )


def test_labels_do_not_affect_features(events: pd.DataFrame) -> None:
    flipped = events.assign(is_fraud=1 - events["is_fraud"])
    pd.testing.assert_frame_equal(
        compute_offline_features(events), compute_offline_features(flipped)
    )


def test_same_timestamp_events_do_not_see_each_other(events: pd.DataFrame) -> None:
    feats = compute_offline_features(events)
    tied = feats.iloc[10:15]
    # All five tied events share customer and timestamp, so their history is identical.
    assert tied["cust_txn_count_1h"].nunique() == 1
    assert tied["cust_txn_count_30d"].nunique() == 1


def test_window_boundaries_are_half_open() -> None:
    t0 = pd.Timestamp("2020-01-01 12:00", tz="UTC")
    df = pd.DataFrame(
        {
            "event_id": ["a", "b", "c"],
            "event_time": [t0, t0 + pd.Timedelta(hours=1), t0 + pd.Timedelta(hours=1, seconds=1)],
            "customer_id": ["cus_0000000000000001"] * 3,
            "category": ["home"] * 3,
            "channel": ["unspecified"] * 3,
            "amount": [10.0, 20.0, 30.0],
        }
    )
    f = compute_offline_features(df)
    # b at t0+1h: a is exactly 1h earlier -> inside [t-1h, t).
    assert f.loc[1, "cust_txn_count_1h"] == 1
    # c at t0+1h+1s: a is outside the 1h window, b is inside.
    assert f.loc[2, "cust_txn_count_1h"] == 1
    assert f.loc[2, "cust_txn_count_24h"] == 2
    assert f.loc[0, "cust_txn_count_24h"] == 0
    assert math.isnan(f.loc[0, "seconds_since_prev_txn"])
    assert f.loc[2, "seconds_since_prev_txn"] == 1.0
    assert f.loc[2, "cust_amt_mean_30d"] == 15.0


def test_online_ignores_overfetched_history() -> None:
    t = pd.Timestamp("2020-02-01", tz="UTC").to_pydatetime()
    old = HistoryEvent(to_epoch_us(t - timedelta(days=31)), 500.0, "home")
    future = HistoryEvent(to_epoch_us(t + timedelta(seconds=1)), 500.0, "home")
    f = compute_online_features(t, 10.0, "home", "unspecified", [old, future])
    assert f["cust_txn_count_30d"] == 0
    assert math.isnan(float(f["cust_amt_mean_30d"]))


def test_encode_rejects_unknown_category() -> None:
    numeric = {name: [0.0] for name in NUMERIC_FEATURES}
    with pytest.raises(ValueError, match="Unknown category"):
        encode(numeric, {"category": ["crypto"], "channel": ["unspecified"]})


def test_encode_column_order(events: pd.DataFrame) -> None:
    feats = compute_offline_features(events.head(50))
    x = encode(
        {n: feats[n].to_numpy() for n in NUMERIC_FEATURES},
        {"category": feats["category"].to_numpy(), "channel": feats["channel"].to_numpy()},
    )
    assert x.shape == (50, len(MODEL_COLUMNS))
    cat_cols = [i for i, c in enumerate(MODEL_COLUMNS) if c.startswith("category=")]
    assert np.all(x[:, cat_cols].sum(axis=1) == 1)
