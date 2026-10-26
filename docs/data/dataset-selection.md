# Dataset selection (first domain: card payments)

## Candidates

| | ULB Credit Card Fraud (Kaggle/OpenML) | PaySim (Kaggle) | IEEE-CIS (Kaggle competition) | **Sparkov card transactions** (Kaggle `kartik2112`) |
|---|---|---|---|---|
| Provenance | Real European card data, 2 days, 2013 | Synthetic, agent-based from a mobile-money log | Real Vesta e-commerce data | Synthetic, from the MIT-licensed [Sparkov generator](https://github.com/namebrandon/Sparkov_Data_Generation) |
| Labels | Real chargeback-based | Simulated | Real, with known label-definition quirks | Simulated fraud episodes |
| Features | `Time`, `Amount`, 28 anonymous PCA components | step (hour), type, amount, origin/destination balances | ~430 partly anonymized columns | timestamp, card, merchant, category, amount, customer home location/demographics |
| Entity ID | **None** | Origin accounts almost never repeat | Must be reconstructed (no official ID) | **Card number (999 cards, ~1,850 txns each)** |
| Time span | 48 hours | 30 simulated days | ~6 months, relative seconds | **24 months, real calendar timestamps** |
| Prevalence | 0.17% | 0.13% | 3.5% | 0.52% |
| License | DbCL v1.0 | CC BY-SA 4.0 | Competition rules: competition use only | Listed on Kaggle as CC0 (verify on the dataset page); generator is MIT |
| Leakage risk | Low but nothing behavioral to leak | **High**: balance fields encode fraud outcome (accounts emptied), `isFlaggedFraud` | Moderate | Moderate: generator artifacts (see below) |
| Replay suitability | Poor (no entity, 2 days) | Weak (no repeat customers) | Licensing blocks redistribution/derived demo | **Good**: per-customer chronological stream |

## Decision

**Sparkov card transactions.** It is the only candidate that simultaneously has (a) a stable entity
identifier with long per-entity history, (b) real calendar timestamps over two years, and (c) a
permissive license. Those three properties are exactly what the platform needs to demonstrate:
point-in-time behavioral features, chronological validation, and a replayable event stream.

The cost of this choice is that **the labels and the fraud behavior are synthetic**. Results on this
data say the pipeline is correct; they do not say the model would detect real fraud. This is
stated wherever metrics appear.

## Acquisition and verification

Kaggle requires login, so `scripts/download_dataset.sh` fetches an unofficial Hugging Face mirror
(`pointe77/credit-card-transaction`, whose own license tag is the uploader's claim, not the
source's). The mirror was accepted only after it matched the Kaggle original exactly:

| Check | Train file | Test file |
|---|---|---|
| Rows | 1,296,675 | 555,719 |
| Fraud rows | 7,506 | 2,145 |
| First / last timestamp | 2019-01-01 00:00:18 / 2020-06-21 12:13:37 | 2020-06-21 12:14:25 / 2020-12-31 23:59:34 |
| Duplicate `trans_num` | 0 | 0 |
| SHA-256 (pinned) | `1a100d7d…2da59f9f` | `05fbcc2d…ee60574f` |

The mirror has one extra column (`merch_zipcode`), which ingestion never reads. Users with Kaggle
access can ingest `fraudTrain.csv`/`fraudTest.csv` directly; the raw schema is identical.

## What ingestion keeps, drops and derives

| Field | Treatment | Why |
|---|---|---|
| `cc_num` | HMAC-SHA256 with a secret key → `cus_<16 hex>`; PAN never written | Pseudonymize; keyed so the small PAN space cannot be brute-forced |
| `first`, `last`, `street`, `city`, `zip`, `dob`, `gender`, `job`, `lat/long`, `city_pop`, `merch_lat/long` | **Never loaded** | Data minimization; gender/age are protected attributes |
| `state` | Kept as `customer_region` (2-letter, coarse) | Permits state-level analysis without precise location |
| `merchant` | `fraud_` prefix stripped → `merchant_id` | The prefix is on **every** row (generator artifact); not a label signal |
| `category` | Kept; drives `channel` | — |
| `channel` | Derived: `*_net` → card_not_present, `*_pos` → card_present, else `unspecified` | No explicit channel column exists; we do not guess for other categories |
| `currency` | `USD` | **Assumption**: not stated by the source (simulates US customers) |
| `trans_date_trans_time` | Pinned to UTC as the dataset clock | **Source has no timezone**. `unix_time` is ignored: it is offset from the timestamp column |
| `is_fraud` | Target | — |

Outputs: `card_transactions.parquet` (1,852,394 rows, 999 customers, 9,651 fraud),
`dataset_manifest.json` (dataset version `ef8be78c6f87` = hash of input hashes + ingestion
version), `data_quality_report.json` (missing values: 0; duplicate ids: 0; amount 1.00–28,948.90,
median 47.52; 3×IQR outliers reported but retained, since large amounts are legitimate signal).

## Target, temporal properties, imbalance

* **Target:** `is_fraud` per transaction (binary). Prediction objective: P(fraud | transaction,
  customer history strictly before it).
* **Imbalance:** 0.52% overall; train window 0.55%, validation 0.59%, test 0.39%. The test window's
  lower prevalence is itself a distribution shift the evaluation must survive.
* **Temporal structure:** fraud occurs in short per-customer bursts (a compromised card is used many
  times in a day or two). This makes velocity features strong and makes **row-level random splits
  and row-level bootstrap invalid** — a burst would be split across train and test.

## Known limitations and leakage risks

1. **Synthetic fraud is easy.** Fraud profiles use different amount/category distributions from
   legitimate profiles (e.g. fraudulent `grocery_pos` amounts centred near $350). The trained
   model's largest gain feature is `category=grocery_pos`. Expect real-world performance to be far
   lower; do not quote these metrics as real-world fraud detection capability.
2. **No authentication, device, decline or login data** — the account-takeover, device-change and
   failed-attempt features in the product brief are **not implemented**, because the data cannot
   support them. They belong to other domain adapters with other datasets.
3. **Label timing is unknown.** The data gives no chargeback/confirmation dates, so delayed-label
   effects cannot be simulated faithfully from this source.
4. **Same customers in train and test.** This mirrors production (known cards) but means results do
   not measure cold-start performance on new cards.
5. **Timezone unknown.** Hour-of-day features use the dataset clock; if real clients send true UTC
   times, the hour features would shift. The API contract requires timezone-aware timestamps.

## Replay suitability

Events are ordered by `(event_time, event_id)`, each has a unique id, and per-customer history is
long, so the test window can be replayed through the API in time order with a 30-day backfill
providing the same lookback the offline features had. Replayed events are stored as
`source='replay'`; backfilled history as `source='backfill'`.
