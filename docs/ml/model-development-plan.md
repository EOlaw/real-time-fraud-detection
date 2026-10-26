# Model development plan

Items marked ✅ are implemented and were executed for model `card-fraud-xgb@1.0.0`
([model card](model-card-card-fraud-xgb-1.0.0.md)); ⏳ are planned for Phase 3/9.

## 1. Features (spec `card-v1`) ✅

**Point-in-time rule.** For an event at time *t* for customer *c*, history = other events of *c*
with `event_time < t` (strict); a window *w* covers `[t − w, t)`. Same-timestamp events never see
each other, so features do not depend on arrival order within a timestamp. Labels are never inputs.

| Group | Features |
|---|---|
| Amount | `log_amount` |
| Time (dataset clock) | `hour_of_day`, `hour_sin`, `hour_cos`, `day_of_week`, `is_night` (22:00–05:59) |
| Velocity | `cust_txn_count_{1h,24h,7d,30d}`, `cust_amt_sum_24h`, `seconds_since_prev_txn` |
| Customer baseline | `cust_amt_mean_30d`, `cust_amt_std_30d`, `amt_zscore_30d` (std floored at 1.0), `amt_to_mean_ratio_30d` |
| Merchant pattern | `cust_same_category_count_30d` |
| Categorical (one-hot, fixed vocab) | `category` (14), `channel` (3) |

Missing history stays `NaN` (XGBoost learns a default direction; sklearn baselines impute with
median + missing indicators). Out-of-vocabulary categories are rejected, not zero-encoded.

**Two implementations, one definition.** `features/offline.py` (vectorized: per-customer binary
search + prefix sums) and `features/online.py` (direct filtering over fetched history) are written
independently. Verification:

* unit parity test on synthetic data with ties and bursts (rel. tol. 1e-9);
* leakage test: changing or adding *future* events leaves earlier features bit-identical;
* label-independence test: flipping all labels leaves features unchanged;
* **real-data parity through the HTTP API**: 200 replayed test-window events, max relative feature
  difference 1.7e-12, score difference 0.0.

## 2. Splits ✅

Chronological on the dataset clock (UTC):

| Window | Range | Rows | Fraud | Use |
|---|---|---|---|---|
| Warm-up | 2019-01-01 → 2019-02-01 | — | — | History only (30-day windows would be truncated) |
| Train | 2019-02-01 → 2020-03-01 | 972,318 | 5,393 | Fit |
| Validation | 2020-03-01 → 2020-06-21 12:14 | 271,832 | 1,607 | Model selection, threshold, operating points, policy thresholds |
| Test | 2020-06-21 12:14 → 2020-12-31 | 555,719 | 2,145 | Scored once after all decisions; exactly the published `fraudTest.csv` |

Customers appear in all windows (as in production). An automated test proves that **flipping every
test label changes no training-time decision** (selection, threshold, model bytes, validation
metrics) while it does change the reported test metrics.

## 3. Candidates and imbalance handling

| Candidate | Imbalance handling | Status |
|---|---|---|
| `DummyClassifier(prior)` | — (prevalence baseline: AP ≈ prevalence) | ✅ |
| Logistic regression | `class_weight=balanced`, impute + scale | ✅ |
| Random forest | `class_weight=balanced_subsample`, native NaN | ✅ |
| XGBoost | none (keeps probabilities calibrated) | ✅ |
| XGBoost weighted | `scale_pos_weight = neg/pos` | ✅ (to test, not assume, reweighting) |
| LightGBM | — | ⏳ only if XGBoost tuning plateaus; it would be a near-duplicate otherwise |
| Under/oversampling, SMOTE | Inside training folds only, never before splitting | ⏳ Phase 3 comparison; the class-weight experiment already showed no meaningful AP gain |
| Isolation Forest / robust z-scores | Separate unsupervised *anomaly* score, never mixed into P(fraud) | ⏳ Phase 3 (behavioral analytics) |
| Optuna tuning | Time-series CV within train only; test untouched | ⏳ Phase 3 |

## 4. Selection rule ✅

Highest **validation average precision** among servable candidates; candidates within **0.005 AP**
are considered tied and the tie goes to the lower **validation Brier score**. Rationale: risk bands
are defined on probabilities, so calibration matters once ranking quality is equivalent. This rule
chose unweighted XGBoost (AP 0.9777, Brier 0.00058) over the weighted variant (AP 0.9781, Brier
0.00169) — a 0.0004 AP difference is noise, a 3× Brier difference is not.

Only XGBoost artifacts are servable (JSON model format; no pickle). If a non-servable model ever wins
on validation, the report says so explicitly instead of silently shipping the runner-up.

## 5. Thresholds and operating points ✅

* `decision_threshold` = max-F2 on validation (recall weighted 4× precision; missed fraud is
  assumed costlier than a review — an assumption, not a measured cost).
* Operating points at alert budgets 0.1%, 0.25%, 0.5%, 1%, 2% on validation.
* The decision policy (`ml/configs/decision_policy.yaml`) takes `challenge_at`, `review_at` and
  `decline_at` from those validation points and is **bound to the model version**; the API refuses
  to start if they disagree.

Expected-cost optimization is ⏳: it needs defensible per-case costs (review cost, average loss per
missed fraud, customer friction) that this dataset cannot provide.

## 6. Evaluation criteria ✅

Accuracy is never reported. Reported on validation (all candidates) and test (selected model, plus
post-hoc ranking metrics for all candidates, labeled as such):

* AP, ROC-AUC, prevalence baseline;
* precision, recall, F1, F2, FPR, FNR, confusion matrix, alert rate at the validation threshold;
* precision/recall at top-K where K = 0.5% of traffic (review capacity);
* calibration table, Brier, ECE;
* channel segments;
* 95% **customer-cluster bootstrap** CIs (200 resamples) for AP, precision and recall — resampling
  customers, not rows, because a fraud burst is many correlated rows.

## 7. Calibration ✅ / ⏳

The unweighted model is already well calibrated on test (ECE 0.00017). Post-hoc calibration
(isotonic/Platt on a dedicated calibration window carved from the end of train) is ⏳ and will be
added only if a future model degrades calibration — fitting it on validation would contaminate
threshold selection.

## 8. Delayed labels, label bias, drift ⏳

* Training labels will be cut off at `train_end − label_maturity` once label timestamps exist
  (Phase 8 case outcomes carry `labeled_at` and provenance).
* Analyst dispositions are *labels with provenance*, not automatic ground truth: "inconclusive" is
  excluded, and reviewed-only populations are biased toward high scores (selection bias), so
  retraining will weight or sample accordingly.
* Monitoring will separate unlabeled signals (score/feature drift) from label-matured performance.
  Drift triggers a *retraining recommendation*, never automatic promotion.
