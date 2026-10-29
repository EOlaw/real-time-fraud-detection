# Model monitoring: drift, label-matured performance and promotion gates (Phase 9)

Code: `apps/api/fraud_api/monitoring/` (`drift.py` statistics, `reference.py` reference
profiles, `job.py` monitoring runs, `gates.py` promotion gates, `cli.py`, `api.py`).
Calculation version: **`drift-v1`**, stored with every reference, run and metric.

## What is compared with what

| Side | Data | Notes |
|---|---|---|
| **Reference** | The model's **own validation window** of its **own dataset version** | Validation is the data its thresholds were chosen on. For training runs the window comes from `splits.validation.time_range` (inclusive); for experiment runs from `training_config.experiment.splits` (`[train_end, validation_end)`). |
| **Current** | Stored assessments in a window of `scored_at` | Only assessments **scored by the monitored model version**. Others are counted as `excluded_other_model_versions`; degraded assessments (no score) feed `degraded_rate` only. |

**Building a reference** (`build-reference`) refuses to run unless all three checks pass:
- the canonical dataset's version equals the artifact's `dataset_version`;
- the artifact hash equals the registry's;
- the number of events in the window equals the manifest's recorded validation row count.

The reference uses the same offline point-in-time features as training, and stores aggregates
only:
- per numeric feature: deciles, bin proportions, a 101-point quantile grid, the exact CDF at that
  grid, and the missing rate;
- per categorical feature: proportions over the closed vocabulary plus `__other__` and
  `__missing__`;
- score bins and quantiles;
- the model-bound policy's decision mix and alert rate.

References are append-only. Rebuilding an identical profile is a no-op; a different profile for
the same model and calculation version is refused.

At monitoring time, current values come from the feature snapshot stored with each assessment
(`risk_assessments.features`). No raw payloads, customer or merchant identifiers are read.

## Statistics

| Metric | Definition |
|---|---|
| PSI | `Σ (c − r)·ln(c / r)` over the reference bins. Empty bins are floored at 1e-4 and renormalised. **Missing values are their own bin**, so a change in missingness is drift, not imputed away. |
| Numeric bins | Reference deciles of non-missing values, duplicates removed (point masses get fewer bins), plus the missing bin |
| Categorical bins | The model's vocabulary (feature spec) plus `__other__` (unseen values) and `__missing__` |
| KS | `max |F_cur(q) − F_ref(q)|` on the reference's 101-point quantile grid, using the reference's **exact** CDF at each grid value (needed for ties in binary and count features). Significance: `1.628·sqrt((n+m)/(n·m))` (α = 0.01). |
| Score | PSI and KS of the model probability; PSI carries a **bootstrap** 95% percentile interval (200 resamples of the current window, reference fixed, seed derived from the run identity so reruns are identical) |
| Policy decision mix | PSI over allow / challenge / review / decline |
| Policy alert rate | Current rate with a Wilson 95% interval; difference from the reference with a normal-approximation 95% interval; ratio to the reference |
| Operational | `degraded_rate`, `rule_escalation_rate`, `final_alert_rate` (Wilson intervals, informational), `excluded_other_model_versions` |
| Segments | `all`, plus `source:live|replay|simulated` for score PSI and alert rate when the segment is non-empty |

## Thresholds and minimum samples

These are **conventions, not values tuned on this data**:
- **PSI** (common industry practice): < 0.10 stable (`ok`), 0.10–0.25 `warn`, ≥ 0.25 `alert`.
- **KS:** `warn` ≥ 0.05 and `alert` ≥ 0.10, in both cases only when significant at α = 0.01. With
  large samples, tiny differences are significant, so an effect-size floor is needed.
- **Alert-rate ratio:** `warn` outside [0.67, 1.5] and `alert` outside [0.5, 2.0], only when the
  95% interval of the difference excludes 0.
- **Minimum sample:** fewer than **500** current values → `insufficient_data`, never `ok`. An empty
  window is `insufficient_data` with recommendation `none`.

**Run status** is the worst status among the thresholded unlabeled metrics on segment `all`.
**Recommendation:** `alert` → `retraining_review`, `warn` or `failed` → `review`, otherwise
`none`. Monitoring **never** retrains, promotes or changes serving.

## Label-matured performance (labeled metrics)

This reuses `labels.metrics.performance`, filtered to the monitored model version:
- **Cohort:** selected by transaction **event time**; `--label-cohort-start/--label-cohort-end`,
  defaulting to the run window.
- **Mature transactions only:** event time ≤ `labels_as_of − maturity_days` (default 30).
- **Labels known at `labels_as_of`:** a label superseded by a correction known by then is
  replaced by that correction.
- **Usable labels:** `fraud` or `legitimate`, `confirmed` confidence (`probable` only with
  `--include-probable`). Unlabeled, inconclusive and probable-excluded transactions are
  **counted and excluded, never treated as legitimate**.
- **Stored metrics:** `precision_*`, `recall_*` and `false_positive_rate_*` for the final and the
  policy decision, plus `label_coverage`, all with Wilson intervals. Each is `insufficient_data`
  unless there are ≥ **200** usable labels and ≥ **20** fraud labels.
- **Caveat:** stored with every labeled metric. The labeled subset is selected (analysts review
  alerts, chargebacks skew toward fraud, dataset labels are synthetic), so it is not unbiased
  ground truth, and recall here is recall among labeled fraud.

Unlabeled signals and labeled metrics are separate rows (`labeled = false/true`) and separate
tables in the UI.

## Persistence and idempotency

**Rows:**
- `monitoring_references`: append-only.
- `monitoring_runs`: one row per (scope, model version, window, calculation version), with a
  summary, status, recommendation and acknowledgement.
- `monitoring_metrics`: one row per (run, metric, segment), with value, interval, sample and
  reference sizes, `labeled`, status, thresholds and details.

**Reruns:** running again updates the same rows with identical values. Acknowledgements are kept
when the status is unchanged and cleared when it changes. Runs and metrics cannot be deleted
(trigger).

**Scopes:** `organization` runs are visible to that tenant (`GET /api/v1/monitoring/runs`).
`global` runs aggregate all tenants and are operator-only (CLI).

**Audit actions:** `monitoring.reference`, `monitoring.run`, `monitoring.acknowledge`.

## Promotion gates (`fraud_api.monitoring.cli gates|promote`)

| Gate | Passes when |
|---|---|
| artifact, policy, threshold, parity, decisions | Phase 3 gates (hash, binding, validation threshold, online/offline parity, decisions) |
| feature_contract | Candidate feature columns equal the serving code and the incumbent's; same feature spec version |
| reproducibility | Manifest dataset version equals the canonical data; git commit and training config recorded; registry (if it knows the version) has the same hash |
| reference | A reference profile exists for the candidate's exact bytes |
| min_samples | ≥ 1,000 stored scored assessments in the evidence window; ≥ 200 mature eligible labels with ≥ 20 fraud |
| drift | The candidate's scores on the stored feature snapshots have PSI < 0.25 against its own reference, and max feature PSI < 0.25 |
| performance | On mature eligible labels: candidate AP ≥ incumbent AP − 0.02, and candidate ECE ≤ 0.02 (labeled subset; caveat applies) |
| monitoring | No unacknowledged `alert` or `failed` run of the serving model in the last 30 days |

Every gate **fails closed**: an exception, missing reference or insufficient sample is a failed
gate with a reason.
- **`gates`:** read-only.
- **`promote --apply --operator NAME`:** runs every gate and refuses on any failure. It then
  repoints `ml/artifacts/current`, installs the bound policy (the previous one is archived), and
  writes a `model.promote` audit record with the operator and each gate's result.
- **`rollback --operator NAME`:** repoints to an earlier artifact after its artifact and policy
  gates pass, and writes `model.rollback`.
- Serving changes only when API instances and workers restart.
- The legacy `fraud_api.cli promote-model --apply` (artifact gates only) is retired and refuses.

## Scheduling

`python -m fraud_api.monitoring.cli run --last-hours 24 --all-orgs --every 3600` recomputes the
last 24 whole UTC hours every hour (whole hours, so repeats within an hour update the same run);
cron with `--last-hours` is equally suitable. There is no hosted scheduler: Phase 10 would run this as a
scheduled task, with Prometheus alerting on its exit status.
