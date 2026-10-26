# Model card — `card-fraud-xgb@1.0.0`

All numbers below were produced by `make train` on 2026-10-26 and are copied from
`ml/artifacts/card-fraud-xgb-1.0.0/evaluation.json`. Re-running training with the same data and
config reproduces the artifact byte-for-byte (SHA-256 `fa211a778e02…`, verified with two runs).

> **Read this first.** The training data is *synthetic* (Sparkov). Its fraud episodes are drawn
> from distributions that differ sharply from legitimate spending, which makes them much easier to
> detect than real fraud. These results demonstrate a correct, leakage-checked pipeline. They are
> **not** evidence of real-world fraud detection performance.

| | |
|---|---|
| Domain | card_payments |
| Algorithm | XGBoost (hist), 500 trees, depth 6, lr 0.05, no class weighting |
| Features | spec `card-v1`, 34 columns ([plan](model-development-plan.md#1-features-spec-card-v1-)) |
| Dataset version | `ef8be78c6f87` |
| Artifact | `model.json` (XGBoost JSON, 2.2 MB) + `manifest.json`; no pickle |
| Decision threshold | 0.2120 (max F2 on validation) |
| Intended use | Sandbox demonstration of real-time risk scoring; decisions are simulated |
| Not intended for | Blocking real payments; any domain other than card payments; real-world performance claims |

## Validation (model selection)

Validation: 271,832 transactions, 1,607 fraud (prevalence 0.59%). K = 0.5% of traffic = 1,359.

| Candidate | AP | ROC-AUC | Brier | Precision@K | Recall@K |
|---|---|---|---|---|---|
| Dummy (prior) | 0.0059 | 0.500 | 0.00588 | 0.008 | 0.007 |
| Logistic regression (balanced) | 0.3993 | 0.9875 | 0.03603 | 0.486 | 0.411 |
| Random forest (balanced subsample) | 0.9465 | 0.9982 | 0.00237 | 0.948 | 0.801 |
| **XGBoost** (selected) | **0.9777** | 0.9996 | **0.00058** | 0.991 | 0.838 |
| XGBoost, `scale_pos_weight` | 0.9781 | 0.9993 | 0.00169 | 0.987 | 0.835 |

Class weighting did not meaningfully change ranking quality (+0.0004 AP) and tripled the Brier
score, so the unweighted model was selected (see the selection rule).

Validation operating points (used to set the decision policy):

| Alert budget | Threshold | Precision | Recall |
|---|---|---|---|
| 0.10% | 0.9998 | 1.000 | 0.169 |
| 0.25% | 0.9985 | 1.000 | 0.423 |
| 0.50% | 0.8851 | 0.991 | 0.838 |
| 1.00% | 0.0122 | 0.581 | 0.982 |
| 2.00% | 0.0018 | 0.294 | 0.996 |

## Held-out test (scored once, after selection)

Test: 555,719 transactions, 2,145 fraud (prevalence 0.386%), 2020-06-21 → 2020-12-31.

| Metric | Value | 95% CI (customer-cluster bootstrap, 200 resamples) |
|---|---|---|
| Average precision | **0.955** | 0.946 – 0.963 |
| ROC-AUC | 0.9993 | — |
| Precision @ 0.212 | 0.876 | 0.856 – 0.892 |
| Recall @ 0.212 | 0.904 | 0.885 – 0.921 |
| F1 / F2 | 0.890 / 0.898 | — |
| False-positive rate | 0.049% | — |
| False-negative rate | 9.6% | — |
| Alert rate | 0.398% | — |
| Precision / recall @ top 0.5% (K = 2,779) | 0.729 / 0.945 | — |
| Brier / ECE | 0.00059 / 0.00017 | — |

Confusion matrix at the validation threshold: TP 1,939 · FP 274 · FN 206 · TN 553,300.

Prevalence baseline: a random ranker has AP ≈ 0.0039 on this window.

**Segments by channel (test):**

| Channel | Transactions | Fraud | AP | Precision | Recall |
|---|---|---|---|---|---|
| card_not_present | 88,572 | 814 | 0.975 | 0.865 | 0.932 |
| card_present | 136,918 | 770 | 0.979 | 0.903 | 0.938 |
| unspecified | 330,229 | 561 | 0.883 | 0.856 | 0.816 |

The `unspecified` channel (categories with no `_net`/`_pos` suffix) is materially weaker.

**Post-hoc test AP for every candidate** (reported for comparison only; not used for any decision):
dummy 0.0039, logistic 0.295, random forest 0.912, XGBoost 0.955, XGBoost weighted 0.957.

**Calibration (test):** predicted vs. observed fraud rate per score bin: 0.1–0.2: 0.14 vs 0.22;
0.4–0.5: 0.44 vs 0.40; 0.8–0.9: 0.85 vs 0.80; 0.9–1.0: 0.99 vs 0.99. Mid-range bins hold few
transactions (52–247), so their observed rates are noisy.

## What the model relies on

XGBoost gain importance (share of total gain; this is *not* SHAP and not causal):

| Feature | Share |
|---|---|
| `category=grocery_pos` | 0.415 |
| `log_amount` | 0.095 |
| `cust_amt_sum_24h` | 0.091 |
| `category=gas_transport` | 0.068 |
| `hour_cos` | 0.056 |
| `is_night` | 0.046 |
| `cust_txn_count_24h` | 0.020 |

The dominance of `category=grocery_pos` reflects how the generator parameterizes fraud profiles,
not a property of real fraud. SHAP local/global explanations are Phase 3.

## Known behaviours and limitations

* **Cold-start customers:** a first-ever transaction has no history (velocity counts 0, baseline
  features null). A smoke test of a brand-new customer spending $842.17 online at 23:00 scored
  0.99999 (critical). High scores on first-seen cards with large night-time online amounts are
  expected here and would need product review before any real use.
* Performance on new card populations, other regions, other currencies or real fraud is unknown.
* Labels are taken as ground truth; real chargeback labels are delayed and noisy.
* The policy's `decline_at` is sandbox-only and must never drive irreversible actions without a
  validated, approved production policy.
