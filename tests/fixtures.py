"""Deterministic synthetic fixtures for tests.

These are test fixtures only. They are not the training dataset and no reported metric is
computed from them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from fraud_ml.domains.card_payments import MERCHANT_CATEGORIES, derive_channel


def synthetic_events(n: int = 2_000, n_customers: int = 25, seed: int = 7) -> pd.DataFrame:
    """Canonical-schema events with bursts, same-timestamp ties and a learnable fraud signal."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2020-01-01", tz="UTC")
    offsets = np.sort(rng.integers(0, 90 * 24 * 3600, size=n))
    # Force some exact timestamp ties and tight bursts.
    offsets[10:15] = offsets[10]
    customers = rng.integers(0, n_customers, size=n)
    customers[10:15] = customers[10]
    categories = rng.choice(MERCHANT_CATEGORIES, size=n)
    amounts = np.round(rng.gamma(2.0, 40.0, size=n) + 1.0, 2)
    is_fraud = np.zeros(n, dtype=int)
    fraud_idx = rng.choice(n, size=max(n // 50, 5), replace=False)
    is_fraud[fraud_idx] = 1
    amounts[fraud_idx] = np.round(amounts[fraud_idx] * 8 + 300, 2)
    return pd.DataFrame(
        {
            "event_id": [hashlib.md5(f"{seed}-{i}".encode()).hexdigest() for i in range(n)],  # noqa: S324
            "event_time": start + pd.to_timedelta(offsets, unit="s"),
            "customer_id": [f"cus_{c:016x}" for c in customers],
            "merchant_id": "test_merchant",
            "category": categories,
            "channel": [derive_channel(c) for c in categories],
            "amount": amounts,
            "currency": "USD",
            "customer_region": "NY",
            "is_fraud": is_fraud,
            "source_file": "synthetic_fixture",
        }
    )


KEY = b"k" * 32
PAN_A, PAN_B = "4111111111111111", "5500000000000004"
# Raw values that must never appear in any output, report, stdout/stderr or log record.
SENSITIVE = [PAN_A, PAN_B, "Janeprivate", "Doeprivate", "1 Secret Street", "1980-07-14"]


def raw_frame(n: int = 8, start: str = "2019-01-01 00:00:00") -> pd.DataFrame:
    """Raw Sparkov-shaped rows, including PII columns ingestion must never read."""
    t0 = pd.Timestamp(start)
    times = [t0 + pd.Timedelta(minutes=i) for i in range(n)]
    times[2] = times[1]  # a timestamp tie, to check deterministic ordering
    return pd.DataFrame(
        {
            "Unnamed: 0": range(n),
            "trans_date_trans_time": [t.strftime("%Y-%m-%d %H:%M:%S") for t in times],
            "cc_num": [PAN_A if i % 2 == 0 else PAN_B for i in range(n)],
            "merchant": ["fraud_Acme"] * n,
            "category": (["grocery_pos", "shopping_net", "travel", "home"] * n)[:n],
            "amt": [10.5, 20.0, 300.0, 4.99, 1250.75, 8.0, 99.99, 15.25][:n],
            "first": ["Janeprivate"] * n,
            "last": ["Doeprivate"] * n,
            "street": ["1 Secret Street"] * n,
            "dob": ["1980-07-14"] * n,
            "state": ["NY"] * n,
            "trans_num": [f"{i:032x}" for i in range(n)],
            "is_fraud": [0, 1, 0, 0, 1, 0, 0, 0][:n],
        }
    )


def write(df: pd.DataFrame, path: Path, natural_types: bool = False) -> Path:
    if path.suffix == ".csv":
        df.to_csv(path, index=False)
    elif natural_types:
        # Parquet as a producer would typically write it: integer card numbers, real timestamps.
        typed = df.assign(
            cc_num=df["cc_num"].astype("int64"),
            trans_date_trans_time=pd.to_datetime(df["trans_date_trans_time"]),
        )
        typed.to_parquet(path, index=False)
    else:
        df.astype({"cc_num": str}).to_parquet(path, index=False)
    return path
