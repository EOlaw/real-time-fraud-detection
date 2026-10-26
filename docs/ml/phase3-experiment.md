# Phase 3 experiment: tuning, imbalance, explanations, anomaly baseline

Code: [`experiment.py`](../../ml/fraud_ml/training/experiment.py) (driver),
[`cv.py`](../../ml/fraud_ml/training/cv.py), [`tuning.py`](../../ml/fraud_ml/training/tuning.py),
[`resampling.py`](../../ml/fraud_ml/training/resampling.py),
[`treeshap.py`](../../ml/fraud_ml/explain/treeshap.py),
[`isolation.py`](../../ml/fraud_ml/anomaly/isolation.py),
[`tracking.py`](../../ml/fraud_ml/tracking.py),
[`promotion.py`](../../apps/api/fraud_api/promotion.py). Config:
[`experiment_card_v2.yaml`](../../ml/configs/experiment_card_v2.yaml).

```bash
make experiment        # ≈ the run reported below; writes ml/reports/phase3/*-experiment.json
make mlflow-ui         # browse runs at http://127.0.0.1:5000 (local sqlite store)
make promote-check     # promotion gates for the candidate; changes nothing
```

> All data is synthetic (Sparkov). The caveats in the
> [model card](model-card-card-fraud-xgb-1.0.0.md) apply to every number below.

## Protocol

1. **Features.** Point-in-time spec `card-v1`, unchanged. No new features in Phase 3, so the
   serving contract and the online/offline parity guarantees are unchanged.
2. **Folds.** Three expanding-window folds, entirely inside the training window
   (2019-02-01 → 2020-03-01). Validation blocks are Sep–Oct 2019, Nov–Dec 2019 and Jan–Feb 2020.
   Training rows within **7 days** before each block are dropped (embargo), so a fraud burst
   straddling a boundary cannot place its early labels in fold-train. Boundaries are explicit
   timestamps in the config.
3. **Tuning.** Optuna TPE (seed 42), 30 sequential trials, and a median pruner after each fold
   (5 start-up trials). The objective is the mean fold average precision. The incumbent's
   parameters are enqueued as **trial 0**; the config check guarantees they lie inside the search
   space, so trial 0 is a faithful baseline. XGBoost seed 42, `hist`, all threads (bit-for-bit
   deterministic on CPU, verified for Phase 1).
4. **Imbalance strategies.** All use the tuned parameters:
   * `none`: unchanged data.
   * `class_weight`: `scale_pos_weight = negatives / positives`.
   * `undersample`: random majority undersampling to 1:10.
   * `smotenc`: SMOTE-NC to 1:10 on primitive features. Category, hour, weekday and the
     "no previous transaction" indicator are categorical (copied from neighbours, never
     interpolated); continuous features are standardized with fold-train statistics. One-hots,
     channel, hour sin/cos, night flag, z-score and amount ratio are then *recomputed*. Counts are
     rounded, and nullability follows the 30-day count. Synthetic rows therefore always form valid
     one-hots and satisfy the feature definitions (tested).

   Each strategy is applied **only to fold-train rows** during CV, then to the full training
   window for the refit. A test spy records every resampling call and asserts its rows are
   exactly a fold-train set or the training window.
5. **Selection** uses the validation window and the unchanged Phase 1 rule: highest AP;
   candidates within 0.005 AP tie, and the tie goes to the lowest validation Brier. The
   incumbent artifact (`card-fraud-xgb@1.0.0`, loaded hash-verified) is a candidate. Its
   validation AP is recomputed and must equal the value recorded in its manifest. A new model is
   saved as a **new version** with a threshold chosen on validation (max-F2) and a candidate
   policy derived exactly as the incumbent's (challenge ≈ 1% validation alert rate, review =
   max-F2, decline ≈ 0.25%).
6. **Test window.** Scored once, after step 5, for the selected model and the incumbent. Paired
   customer-cluster bootstrap CIs (200 resamples) are reported for differences.
7. **Explanations and anomaly baseline.** These come after selection and cannot influence it
   (tested: disabling the anomaly analysis leaves every training decision unchanged).

## Guarantees and the tests that enforce them

| Guarantee | Test (`tests/unit/test_phase3.py`) |
|---|---|
| Folds are chronological, embargoed and inside the training window | `test_folds_are_chronological_embargoed_and_inside_training_window` |
| Tuning ignores rows and labels after `train_end` (labels flipped *and* amounts ×50 → identical trial values) | `test_tuning_ignores_validation_and_test_rows_entirely` |
| Resampling receives only fold-train rows or the training window; CV always scores untouched validation rows | `test_resampling_only_ever_receives_training_rows` |
| SMOTE-NC rows are valid one-hots, channel = f(category), integer hours/counts, monotone windows, correct nullability; originals unchanged | `test_smotenc_rows_respect_feature_definitions` |
| Flipping every test-window label changes no tuning trial, CV score, validation metric, selection, threshold or artifact hash | `test_test_labels_do_not_influence_any_training_decision` |
| Reproducible; anomaly analysis on/off changes nothing | `test_experiment_is_reproducible_and_anomaly_does_not_affect_the_model` |
| New model → new version + bound policy, no overwrite, not promoted; incumbent kept → no artifact or policy | `test_new_model_gets_new_version_bound_policy_and_is_not_promoted`, `test_incumbent_is_kept_when_it_wins` |
| SHAP reconstructs log-odds, sigmoid(margin) = probability; corrupted contributions detected; identifiers refused | `test_shap_contributions_reconstruct_log_odds_and_probability`, `test_broken_contributions_are_detected`, `test_explanations_refuse_identifiers` |
| Anomaly score cannot reach the policy or model; fit takes no labels and refuses label/score columns | `test_anomaly_score_cannot_reach_the_decision_policy_or_model`, `test_anomaly_fit_is_label_free_and_reported_separately` |
| Tracking never contains customer tokens, event ids or secrets; MLflow store verified on disk | `test_tracker_records_contain_no_transaction_data_or_secrets`, `test_guard_rejects_sensitive_values`, `test_mlflow_tracking_end_to_end` |
| Promotion gates pass for a consistent candidate, fail on policy mismatch or tampering; apply is reversible | `test_promotion_gates_pass_and_apply_is_reversible`, `test_promotion_gates_fail_on_mismatch` |

## Explanations

XGBoost's built-in TreeSHAP (`pred_contribs=True`) is exact path-dependent TreeSHAP. The
contributions are in **log-odds**: `base_value + Σ contributions = margin` and
`probability = sigmoid(margin)`. They do not add up in probability space. Every explained batch
is checked against the model's margin (error > 1e-3 raises), and the maximum error is reported.
One-hot columns are summed into `category` and `channel`, which is still exact. Per-transaction
examples show only model inputs (no event id, customer token or raw field). They are written to
`ml/artifacts/<model>/explanations.json` (git-ignored) and are never logged to MLflow. SHAP
values describe the model's behaviour; they are not causal and are not evidence of fraud.

## Anomaly baseline

An Isolation Forest (200 trees, 256 samples per tree) is fit **without labels** on 200,000 random
training-window rows. Inputs are behavioural features: amount, hour, velocity, 24h sum, z-score
and ratio vs the customer's 30-day baseline, recency and same-category count. Heavy-tailed inputs
are log-transformed and missing values are imputed with missing indicators. The output is an
`AnomalyScores` object (unbounded, higher = more isolated, not a probability).
`DecisionPolicy.decide` raises `TypeError` for anything that is not a probability float, and the
anomaly score is never written to model artifacts, selection or policies. Labels are used only
to *evaluate* it.

## MLflow

Local store `sqlite:///ml/mlruns/mlflow.db`, artifacts in `ml/mlruns/artifacts` (git-ignored).

| Logged | Where |
|---|---|
| Parent run `phase3:<model>@<candidate>` | params = full flattened config; tags = dataset version, canonical schema version, parquet SHA-256, feature spec version, baseline model + SHA-256, git commit |
| `folds.json` | fold boundaries and row counts |
| `tuning` | per-trial metrics (step = trial), best params, `trials.json` |
| `cv:<strategy>` | per-fold metrics (step = fold) and means |
| `validation:<candidate>` | AP, ROC-AUC, Brier, ECE, precision/recall@K |
| `selection` | selected candidate, new version, artifact SHA-256, candidate policy; `manifest.json` and the policy file |
| `test`, `explanations`, `anomaly`, `report` | aggregate metrics, global importance, aggregate report |

Never logged: transactions, feature rows, per-transaction explanations, event ids, customer
tokens or secrets. `tracking.guard` rejects any value containing a customer token, a 32-hex
event id or the value of a secret environment variable, and the test suite scans the on-disk
MLflow database and artifacts.

## Results (real data, 2026-10-26)

Run: `make experiment`, 833 s wall time on an Apple M4 Pro (14 cores); git commit `4a10bca`,
dataset `ef8be78c6f87`. Full report: `ml/reports/phase3/card-fraud-xgb-1.1.0-experiment.json`.

**Reproducibility (verified on real data):** a second full run, writing to scratch locations
with MLflow off, produced identical results:
* all 30 trial values and the best parameters
* every fold and validation AP/Brier
* the selection and threshold
* the artifact SHA-256 `2f216f103affdf6f…`, test AP, SHAP global importance and anomaly AP
* the candidate policy

### Folds

| Fold | Train (after 7-day embargo) | Validation block | Train rows | Validation rows |
|---|---|---|---|---|
| 0 | 2019-02-01 → 2019-08-25 | Sep–Oct 2019 | 501,583 | 139,410 |
| 1 | 2019-02-01 → 2019-10-25 | Nov–Dec 2019 | 644,793 | 211,481 |
| 2 | 2019-02-01 → 2019-12-25 | Jan–Feb 2020 | 840,906 | 99,993 |

### Tuning

30 trials (22 completed, 8 pruned). Mean CV AP with the incumbent's parameters (trial 0) was
**0.9677**; the best (trial 27) was **0.9714**: 1000 trees, learning rate 0.0203, depth 7,
subsample 0.681, colsample 0.791, min child weight 1.24, λ 0.285.

### Imbalance strategies (tuned parameters)

| Strategy | Train rows after resampling | CV AP mean ± sd (folds) | CV Brier | Validation AP | Validation Brier | Validation ECE |
|---|---|---|---|---|---|---|
| **none** | 972,318 | **0.9714 ± 0.0113** (0.966, 0.964, 0.984) | **0.00068** | **0.9789** | **0.00056** | 0.00012 |
| class_weight (`scale_pos_weight` 179.3) | 972,318 | 0.9650 ± 0.0184 (0.968, 0.945, 0.981) | 0.00116 | 0.9781 | 0.00134 | 0.00240 |
| undersample 1:10 | 59,323 (912,995 dropped) | 0.9631 ± 0.0140 (0.959, 0.952, 0.979) | 0.00188 | 0.9732 | 0.00174 | 0.00318 |
| SMOTE-NC 1:10 | 1,063,617 (91,299 synthetic) | 0.9572 ± 0.0173 (0.956, 0.940, 0.975) | 0.00100 | 0.9695 | 0.00098 | 0.00160 |

**Conclusion: on this data, no imbalance treatment helped.** Every resampling or weighting
strategy ranked worse on CV and on validation. Undersampling and SMOTE-NC were significantly
worse than the incumbent on validation (paired CIs exclude zero), and all three were worse
calibrated, because changing the class balance shifts predicted probabilities away from true
rates. XGBoost already learns well from the 5,393 natural positives.

### Selection, test, promotion gates

* Selected `tuned:none` as **`card-fraud-xgb@1.1.0`**: a validation-AP tie (+0.0013, within
  0.005) broken by Brier (0.00056 vs 0.00058).
* Test (after the decision): AP 0.9601 vs 0.9553; paired difference **+0.0048
  [+0.0029, +0.0073]**. Recall 0.929 vs 0.904 and precision 0.843 vs 0.876 at the respective
  validation thresholds. Details: [model card 1.1.0](model-card-card-fraud-xgb-1.1.0.md).
* `make promote-check`: **all 5 gates PASS**. 500 real events: max relative feature difference
  4.2e-12, max score difference 0.0; 500 decisions (495 allow, 2 challenge, 1 review,
  2 decline). Nothing was changed.
* End-to-end serving check, without promotion: the API was started against a throwaway database
  with `FRAUD_MODEL_DIR=ml/artifacts/card-fraud-xgb-1.1.0` and the candidate policy, after a
  30-day history import. It reported ready, and 2,000 held-out events replayed with 0 degraded.
  100 parity checks gave max feature difference 6.2e-13 and score difference 0.0; server p50 /
  p95 3.5 / 4.7 ms. The throwaway database was then dropped; the dev database's model registry
  still lists only 1.0.0.

### Explanations

Max TreeSHAP reconstruction error 3.1e-5 log-odds over all explained rows. Global importance,
example explanations and their interpretation are in the
[model card](model-card-card-fraud-xgb-1.1.0.md#explanations-shap).

### Anomaly baseline

| | Validation | Test |
|---|---|---|
| Average precision (prevalence) | 0.120 (0.0059) | 0.067 (0.0039) |
| ROC-AUC | 0.913 | 0.893 |
| Precision / recall @ top 0.5% | 0.190 / 0.161 | 0.120 / 0.156 |
| Spearman correlation with fraud probability | 0.119 | 0.051 |
| Frauds missed by the model at its threshold | 62 | 152 |
| …of which in the anomaly top 0.5% | 9 | 19 |

Analysis only (not deployed): a logistic combiner of `logit(fraud probability)` and the anomaly
score, fit on validation, scored test AP 0.9600 vs 0.9601 for the model alone (difference 0.0000).

**Conclusion:** the anomaly score carries real but weak signal on its own (17× the prevalence
baseline in AP). It is nearly uncorrelated with the fraud model, but it **adds no ranking value
on top of the supervised model** on this data. It flags 19 of the 152 frauds the model misses,
but only inside a 2,779-alert budget at 12% precision, which is not a usable complement. It
remains an unsupervised behavioural signal for analysts: separate, unblended, and absent from the
policy. What it detects: transactions that are unusual relative to overall traffic (large
amounts vs the customer's baseline, bursts, odd hours, long gaps). Limits: unusual is not
fraudulent (large legitimate purchases score high), fraud that looks normal scores low, and
scores are uncalibrated and only meaningful as a ranking.

### MLflow (real run)

16 runs in experiment `card-fraud-phase3`: the parent run, tuning, 4 `cv:*`, 5 `validation:*`,
selection, test, explanations, anomaly and report. The parent run is tagged with dataset version,
parquet hash, feature spec, baseline model and SHA-256, and git commit. The tuning run has 30
metric steps. Artifacts: `folds.json`, `trials.json`, `global_importance.json`, `manifest.json`,
the candidate policy and `experiment_report.json`. A scan of the sqlite store and the artifacts
found 0 customer tokens and 0 secret values. The only 32-hex strings are MLflow's own 16 run
IDs, and none matches a real event ID.
