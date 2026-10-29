# Model development plan

Items marked ✅ are implemented and were executed: Phase 1 for `card-fraud-xgb@1.0.0`
([model card](model-card-card-fraud-xgb-1.0.0.md)) and Phase 3 for the candidate
`card-fraud-xgb@1.1.0` ([model card](model-card-card-fraud-xgb-1.1.0.md),
[experiment](phase3-experiment.md)). Items marked ⏳ are planned.

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
| LightGBM | — | ⏳ not justified: tuned XGBoost improved test AP by only +0.005, so a near-duplicate GBDT is unlikely to change conclusions |
| Random undersampling 1:10, SMOTE-NC 1:10 | Applied to fold-train rows only; SMOTE-NC on primitive features with derived features recomputed | ✅ Phase 3: both **worse** than no resampling (validation AP −0.0044 and −0.0082 vs incumbent, paired CIs exclude 0) and worse calibrated |
| Class weighting (tuned) | `scale_pos_weight = neg/pos` | ✅ Phase 3: AP tie, Brier 2.4× worse |
| Isolation Forest | Separate unsupervised *anomaly* score, never mixed into P(fraud) | ✅ Phase 3: test AP 0.067 standalone, adds 0.0000 AP on top of the model |
| Optuna tuning | 3 expanding folds inside train, 7-day embargo, TPE seed 42, 30 trials, incumbent as trial 0 | ✅ Phase 3: CV AP 0.9677 → 0.9714 |

## 4. Selection rule ✅

Phase 3 reuses this rule unchanged (`select_model(..., candidates=...)`), with the incumbent
artifact as a candidate. Under it, 1.1.0 was selected by the Brier tie-break.

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

## 8. Delayed labels, label bias, drift (labels ✅ Phase 8; drift ✅ Phase 9)

**Two different kinds of labels exist, and they are not interchangeable.**

* **Dataset labels** are the synthetic dataset's `is_fraud` column. The models in this repository
  were trained and evaluated on them (Phases 1 and 3). `fraud_api.cli import-dataset-labels`
  can record them in `fraud_labels` with `source = 'dataset'` and the dataset version in
  `provenance`. They are synthetic: generator artifacts make them easier to predict than real
  fraud, and they carry no information about when a real outcome would have become known.
* **Analyst dispositions, chargebacks and customer reports** (`analyst_review`, `chargeback`,
  `customer_report`) are *observations with provenance*:
  - who recorded them (`labeled_by`), and on which case;
  - how confident they are (`confirmed` or `probable`);
  - when the outcome became known (`labeled_at`), which differs from `recorded_at`.

  They are **not automatically ground truth**. Analysts review a *selected* population (alerts
  and cases), so their labels are biased toward high scores. Chargebacks skew toward fraud.
  `inconclusive` means "not known".

**History is append-only.** A correction is a new label that supersedes the previous one, so
earlier labels stay queryable. "What did we know at time T" is answered by the labels with
`labeled_at ≤ T` that were not superseded by another label known by T.

**Label maturity.** A transaction's label is only meaningful once enough time has passed for its
outcome to be known (real chargebacks arrive weeks later). Label-matured performance
(`GET /api/v1/fraud/insights/performance`) therefore:

1. selects a cohort by transaction **event time** (the window);
2. drops events after `as_of − maturity_days` (default `FRAUD_LABEL_MATURITY_DAYS = 30`) as
   **immature**;
3. uses only labels known at `as_of` (point in time);
4. counts only current `fraud` / `legitimate` labels with `confirmed` confidence. `probable`
   labels are included only with `include_probable=true`; `inconclusive` labels and unlabeled
   transactions are **excluded and counted**, never treated as legitimate;
5. reports precision, recall among labeled fraud, and false-positive rate among labeled
   legitimate, each with numerator and denominator. Final (rule-adjusted) and policy decisions
   are reported separately, together with label coverage and label sources.

Because the labeled subset is selected, these are **not population metrics**: recall here is not
the fraud capture rate, and precision over analyst-reviewed alerts does not transfer to unreviewed
traffic. The response carries this caveat. Unlabeled signals (alert rate and mean score over all
assessments) are reported in a separate block.

**Training (future phases).** Retraining on platform labels will cut labels off at
`train_end − label_maturity`, use only labels known at the training cutoff, and weight or sample
for selection bias. **No model was retrained in Phase 8**, and labels are never model inputs or
features.

**Drift ✅ (Phase 9).** Implemented in `fraud_api.monitoring`; full method in
[monitoring.md](monitoring.md).
- **Reference:** each model's own validation window and dataset version, checked against the
  manifest's row count and artifact hash.
- **Current window:** compared on the feature snapshots stored with each assessment.
- **Statistics:** score PSI/KS (with a bootstrap interval), feature PSI/KS with explicit missing and
  unseen-category bins, policy decision mix and alert rate (Wilson intervals), by source and per
  tenant.
- **Kept separate:** label-matured performance uses the same maturity and cutoff rules as above,
  is stored as separate `labeled` rows with the selection-bias caveat, and needs ≥ 200 usable
  labels and ≥ 20 fraud.
- **Advisory only:** drift **recommends** `review` or `retraining_review` and never promotes or
  retrains automatically.
- **Promotion gates** add the following checks to the Phase 3 gates, and promotion stays an
  explicit, audited, reversible CLI action:
  - feature contract and reproducibility;
  - minimum samples;
  - candidate drift on recent traffic;
  - labeled AP and calibration against the incumbent;
  - no unacknowledged monitoring alerts.

**Real-data finding (synthetic data).** Model `1.0.0` was run on 20,000 held-out events
(2020-06-21 to 06-28) against its validation window (2020-03-01 to 2020-06-21):
- **Score:** stable (PSI 0.006).
- **Features:** `cust_txn_count_30d` drifted (PSI 0.32, alert). Mean 30-day counts rose from 96
  to 120, matching the generator's seasonal volume increase.
- **Recommendation:** `retraining_review`.
- **1.1.0 gates:** for the same reason, 1.1.0 fails the drift gate. Its other evidence gates pass
  (AP 0.940 vs incumbent 0.943, within the 0.02 tolerance).

It was not promoted.
