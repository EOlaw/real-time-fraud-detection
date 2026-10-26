# Model card — `card-fraud-xgb@1.1.0` (candidate, not serving)

**Status: candidate.** Produced by the Phase 3 experiment on 2026-10-26. Serving still uses
`card-fraud-xgb@1.0.0` with `ml/configs/decision_policy.yaml`. This model has a bound candidate
policy (`ml/configs/policies/card-fraud-xgb-1.1.0.yaml`) and passed every promotion gate in
check-only mode, but **it has not been promoted**. Promotion is an explicit decision:
`make promote CANDIDATE=card-fraud-xgb-1.1.0`.

> **Synthetic data.** Trained and evaluated on Sparkov synthetic transactions. The fraud
> episodes are much easier to detect than real fraud. These numbers show the pipeline works and
> allow comparison between models on this data; they say nothing about real-world performance.

| | |
|---|---|
| Parent | `card-fraud-xgb@1.0.0` (SHA-256 `fa211a77…`) |
| Artifact | `ml/artifacts/card-fraud-xgb-1.1.0/model.json`, XGBoost JSON, 6.2 MB, SHA-256 `2f216f103affdf6f73817407b92387f923154c694fa3a59d71c0f6fa5a635b57` |
| Data / features | dataset `ef8be78c6f87`, feature spec `card-v1` (unchanged → same serving contract) |
| What changed | Optuna-tuned hyperparameters only; imbalance strategy `none` (no resampling, no class weights) |
| Hyperparameters | 1000 trees, learning rate 0.0203, max depth 7, subsample 0.681, colsample 0.791, min child weight 1.24, λ 0.285 (1.0.0: 500 / 0.05 / 6 / 0.8 / 0.8 / 1.0 / 1.0) |
| Decision threshold | 0.1338 (max-F2 on validation; 1.0.0: 0.2120) |
| Candidate policy | `card-policy-card-fraud-xgb-1.1.0`: challenge ≥ 0.010788, review ≥ 0.133841, decline (sandbox) ≥ 0.997096 |
| Experiment report | `ml/reports/phase3/card-fraud-xgb-1.1.0-experiment.json`; MLflow experiment `card-fraud-phase3` |

## Why it was selected

The rule is unchanged from Phase 1: highest validation AP, with candidates within 0.005 AP
counted as tied and the tie going to the lower validation Brier score.

| Candidate (validation, 271,832 rows, 1,607 fraud) | AP [95% CI] | AP − 1.0.0 [paired 95% CI] | Brier | ECE | Recall @ top 0.5% |
|---|---|---|---|---|---|
| **tuned, no resampling (→ 1.1.0)** | **0.9789** [0.9717, 0.9852] | +0.0013 [+0.0000, +0.0024] | **0.00056** | 0.00012 | 0.838 |
| tuned, class weight | 0.9781 [0.9700, 0.9844] | +0.0004 [−0.0011, +0.0020] | 0.00134 | 0.00240 | 0.837 |
| tuned, undersample 1:10 | 0.9732 [0.9654, 0.9803] | −0.0044 [−0.0065, −0.0020] | 0.00174 | 0.00318 | 0.831 |
| tuned, SMOTE-NC 1:10 | 0.9695 [0.9587, 0.9773] | −0.0082 [−0.0118, −0.0058] | 0.00098 | 0.00160 | 0.836 |
| incumbent 1.0.0 | 0.9777 [0.9701, 0.9843] | — | 0.00058 | 0.00011 | 0.838 |

**Read this carefully.** On validation, the tuned model and the incumbent are a *tie* under the
selection rule (+0.0013 AP, inside the 0.005 tolerance). The tuned model was chosen because its
Brier score is slightly lower (0.00056 vs 0.00058). The validation evidence alone is weak. The
held-out test results below are stronger, but they were not used for the decision.

The incumbent's recomputed validation AP matched its manifest exactly
(`baseline_reproduced_on_validation: true`).

## Held-out test (scored once, after selection)

555,719 transactions, 2,145 fraud (0.386%). Bootstrap = customer-cluster, 200 resamples.

| Metric | 1.1.0 | 1.0.0 |
|---|---|---|
| Average precision [95% CI] | **0.9601** [0.9509, 0.9682] | 0.9553 [0.9459, 0.9634] |
| AP difference (paired) | **+0.0048 [+0.0029, +0.0073]** | — |
| ROC-AUC | 0.9992 | 0.9993 |
| Threshold (validation-chosen) | 0.1338 | 0.2120 |
| Precision [95% CI] | 0.843 [0.817, 0.862] | 0.876 [0.856, 0.892] |
| Recall [95% CI] | **0.929** [0.915, 0.945] | 0.904 [0.885, 0.921] |
| TP / FP / FN | 1,993 / 371 / 152 | 1,939 / 274 / 206 |
| False-positive rate | 0.067% | 0.050% |
| Alert rate | 0.425% | 0.398% |
| Precision / recall @ top 0.5% | 0.735 / 0.952 | 0.729 / 0.945 |
| Brier / ECE | 0.000546 / 0.000176 | 0.000587 / 0.000171 |

Per channel (AP / recall / precision): card-not-present 0.978 / 0.958 / 0.820; card-present
0.982 / 0.961 / 0.881; unspecified 0.895 / 0.843 / 0.826. The `unspecified` channel remains the
weakest, as for 1.0.0.

**Trade-off if promoted:** at its validation-chosen threshold, 1.1.0 catches 54 more test frauds
(recall +2.5 points) and raises 97 more false alerts (precision −3.3 points, alert rate
+0.027 points). The ranking improvement (AP) is statistically clear on test; whether the
operating point is preferable is a business decision about review capacity.

## Explanations (SHAP)

XGBoost TreeSHAP, in **log-odds**. Max reconstruction error over all explained rows: 3.1e-5
(base value + contributions = margin; sigmoid(margin) = probability). Global importance is the
mean |contribution| on 20,000 random validation rows:

| Feature | All rows | Fraud rows (1,607) |
|---|---|---|
| `log_amount` | 2.50 | 3.43 |
| `cust_amt_sum_24h` | 1.56 | 3.03 |
| `category` (14 one-hots summed) | 0.76 | 1.58 |
| `seconds_since_prev_txn` | 0.38 | 0.48 |
| `amt_to_mean_ratio_30d` | 0.37 | 0.44 |
| `hour_cos` | 0.36 | 0.50 |
| `cust_txn_count_30d` | — | 0.62 |

Phase 1 reported XGBoost *gain* importance, dominated by `category=grocery_pos`. SHAP measures
something different (contribution to each prediction rather than split gain), and here the
amount and 24-hour spend dominate. Neither measure is causal.

Example (validation, highest-scoring fraud, model inputs only): base −5.17 log-odds → margin
+14.74 (p ≈ 1.0). Largest contributions: `category=grocery_pos` +3.38, missing 30-day std +2.61,
`log_amount` 5.72 (≈ $303) +2.54, missing z-score +2.35, no previous transaction +2.02.
**This is a cold-start transaction** (no customer history). As with 1.0.0, a first-ever
transaction with a fraud-typical category and amount scores very high on this synthetic data.

## Known limitations

* Everything in the [1.0.0 model card](model-card-card-fraud-xgb-1.0.0.md#known-behaviours-and-limitations)
  applies (synthetic labels, missing device/login data, unknown timezone, cold start).
* The validation evidence for choosing 1.1.0 is a tie broken by calibration; the test-window
  gain was measured after the decision and is the main evidence of improvement.
* Tuning used three CV folds and 30 trials (8 pruned); a larger budget might find better
  settings, and fold-to-fold variation (CV AP 0.964–0.984) is larger than the tuning gain.
* Twice as many trees: inference latency through the API was unchanged in the measured replay
  (server p50 3.5 ms, p95 4.7 ms over 2,000 events), but the artifact is 3× larger.
