"""Data contracts for raw source data and the canonical (tokenized) transaction table."""

from __future__ import annotations

import pandera.pandas as pa

from fraud_ml.domains.card_payments import CHANNELS, MERCHANT_CATEGORIES

# Raw Sparkov / Kaggle "Credit Card Transactions Fraud Detection" columns we read. Everything else
# in the file (names, street, dob, gender, job, ...) is never loaded.
RAW_COLUMNS: tuple[str, ...] = (
    "trans_date_trans_time",
    "cc_num",
    "merchant",
    "category",
    "amt",
    "state",
    "trans_num",
    "is_fraud",
)

RawTransactionSchema = pa.DataFrameSchema(
    {
        "trans_date_trans_time": pa.Column("datetime64[ns]", nullable=False),
        "cc_num": pa.Column(str, nullable=False, checks=pa.Check.str_matches(r"^\d{8,19}$")),
        "merchant": pa.Column(str, nullable=False),
        "category": pa.Column(str, pa.Check.isin(MERCHANT_CATEGORIES), nullable=False),
        "amt": pa.Column(float, [pa.Check.gt(0), pa.Check.lt(1_000_000)], nullable=False),
        "state": pa.Column(str, pa.Check.str_matches(r"^[A-Z]{2}$"), nullable=False),
        "trans_num": pa.Column(str, pa.Check.str_matches(r"^[0-9a-f]{32}$"), nullable=False),
        "is_fraud": pa.Column(int, pa.Check.isin([0, 1]), nullable=False),
    },
    strict=True,
    coerce=False,
)

# Canonical transaction table produced by ingestion. This is the training *and* replay contract;
# the scoring API's request schema mirrors these fields (minus the label).
CanonicalTransactionSchema = pa.DataFrameSchema(
    {
        "event_id": pa.Column(str, unique=True, nullable=False),
        "event_time": pa.Column("datetime64[us, UTC]", nullable=False),
        "customer_id": pa.Column(str, pa.Check.str_matches(r"^cus_[0-9a-f]{16}$")),
        "merchant_id": pa.Column(str, nullable=False),
        "category": pa.Column(str, pa.Check.isin(MERCHANT_CATEGORIES)),
        "channel": pa.Column(str, pa.Check.isin(CHANNELS)),
        "amount": pa.Column(float, pa.Check.gt(0)),
        "currency": pa.Column(str, pa.Check.eq("USD")),
        "customer_region": pa.Column(str, nullable=False),
        "is_fraud": pa.Column(int, pa.Check.isin([0, 1])),
        "source_file": pa.Column(str, nullable=False),
    },
    strict=True,
    ordered=False,
)
