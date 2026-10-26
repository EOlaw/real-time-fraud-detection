"""Deterministic synthetic fixtures for tests.

These are test fixtures only. They are not the training dataset and no reported metric is
computed from them.
"""

from __future__ import annotations

import hashlib

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
