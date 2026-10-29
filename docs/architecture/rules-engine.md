# Rules engine and analytics (Phase 6)

Tenant rules add deterministic, auditable business logic on top of the model-bound policy. They
are data (validated JSON), not code. The model score and the policy decision are inputs to rules
and are stored unchanged next to the final action.

Code: `apps/api/fraud_api/rules/` (`grammar.py` language, `engine.py` precedence, `service.py`
versions/activation/trace), `apps/api/fraud_api/insights/analytics.py`. Migration `0003`.
API contract: [docs/api/rules-analytics-api.md](../api/rules-analytics-api.md).

## Where rules run

```text
score_transaction (the only scoring path: /fraud/score, /fraud/batch, stream workers)
  idempotency check ── existing id? → return the stored assessment and its stored rule trace
  point-in-time features → model → policy.decide(score)        → risk_score, policy_decision
  load active rule versions (same DB transaction) → evaluate    → decision, rules_status, trace
  INSERT transaction, risk_assessment, rule_evaluations, audit_log  (one commit)
```

Because single requests, batches and stream workers all call `score_transaction`, they evaluate
rules identically. This is tested on synthetic events and was checked on 5,000 real events
(below).

## Condition grammar

```text
condition := predicate
           | {"all": [condition, ...]}     AND, 1..16 children
           | {"any": [condition, ...]}     OR,  1..16 children
           | {"not": condition}
predicate := {"field": <field>, "op": <op>, "value": <literal>}
           | {"field": <nullable field>, "op": "is_null"}
```

Unknown keys, unknown fields, unknown operators, wrong literal types and extra keys are rejected
with `422 invalid_rule` and a list of `{path, message}`. The messages never echo submitted values.
The condition is never `eval`'d. It is compiled into a small immutable tree of `All`, `Any`, `Not`
and `Predicate` nodes, and evaluated by a fixed interpreter. `GET /api/v1/rules/grammar` returns
the same specification in machine-readable form.

**Operators by field type**

| Type | Operators | Literal |
|---|---|---|
| `decimal` | `eq ne lt lte gt gte` | finite JSON number; compared as an exact `Decimal` |
| `number` | `eq ne lt lte gt gte` | finite JSON number (not a boolean, not NaN/∞) |
| `enum` | `eq ne in not_in` | string from the field's closed set; `in` takes 1..100 values |
| `string` | `eq ne in not_in` | 1..200 characters; `in` takes 1..100 values |
| any nullable field | `is_null` | none |

**Field allowlist.** Only facts that exist at scoring time are available, so there are no labels
and nothing from after the event.

| Field | Type | Null? | Source |
|---|---|---|---|
| `amount` | decimal | no | request |
| `category` | enum (14 card categories) | no | request |
| `channel` | enum `card_not_present, card_present, unspecified` | no | derived from category |
| `region`, `merchant_id` | string | yes | request |
| `customer_id` | string | no | request (tokenized) |
| `source` | enum `live, replay, simulated` | no | request label |
| `status` | enum `scored, degraded` | no | scoring outcome |
| `risk_score` | number | yes (degraded) | model probability |
| `risk_level` | enum `low … critical, unknown` | no | policy |
| `policy_decision` | enum `allow, challenge, review, decline` | no | policy |
| `feature.<name>` for the 17 numeric online features | number | yes | point-in-time features, e.g. `feature.cust_txn_count_1h`, `feature.is_night`, `feature.amt_zscore_30d` |

**Limits:**
- nesting depth: 4 combinators;
- size: 32 nodes, 16 children per combinator, 100 values per list, 200 characters per string, 8 KiB of canonical JSON;
- 100 active rules per tenant.

**Three-valued logic.** A comparison on a missing value is *unknown*, not false. Values are missing when the score is degraded, when a feature is undefined (for example, no previous transaction or no 30-day history), or when the region is absent. `all` is false if any child is false, otherwise unknown if any child is unknown. `any` is true if any child is true, otherwise unknown if any child is unknown. `not unknown` is unknown. A rule **matches only when its condition is true**, and the evaluation is recorded as `unknown` when it is unknown. Use `is_null` to target missing values explicitly.

Example:

```json
{"all": [
  {"field": "channel", "op": "eq", "value": "card_not_present"},
  {"field": "amount", "op": "gte", "value": 900},
  {"any": [
    {"field": "feature.is_night", "op": "eq", "value": 1},
    {"field": "feature.cust_txn_count_1h", "op": "gte", "value": 3}
  ]}
]}
```

## Decision semantics

* **Actions:** `flag`, `challenge`, `review`, `decline`. Severity order is
  `allow < challenge < review < decline`. `flag` records a hit and never changes the decision.
  There is **no `allow` action**: rules can escalate the policy decision but can never relax it.
  An allowlist would need a separate, explicitly reviewed mechanism, and it is not built.
* **Evaluation order:** every active rule is evaluated, sorted by `priority` descending (0–1000)
  and then `rule_key` ascending.
* **Final decision:** the most severe of the policy decision, every matched rule's action, and
  the fail-safe action of every errored rule. The result does not depend on the order in which
  rules were loaded, which is unit-tested.
* **`decided_by`:** the first rule in evaluation order whose contribution equals the final
  decision, recorded only when that decision is stricter than the policy decision. Otherwise the
  policy decided and no rule is marked `influenced_decision`.
* **Separately traceable:**
  - `risk_score`: model probability, never modified by rules;
  - `risk_level` and `policy_decision`: the model-bound policy;
  - `decision`: the final action;
  - `rules_status`: `none`, `evaluated` or `error`;
  - `rule_evaluations`: one row per evaluated rule version, with its outcome and
    `influenced_decision`;
  - `reasons`: policy reasons followed by `rule <key>@v<n>: <action>` and
    `rule_error <key>@v<n>: escalated`.
* **No active rules:** `rules_status = none` and `decision = policy_decision`, so existing
  behaviour is unchanged. Migration `0003` backfilled existing rows the same way.

**Fail-safe on rule errors.** An evaluation error raises no exception to the caller. It can come
from a stored condition that no longer compiles (only possible by writing the database directly),
a missing fact, a wrong fact type, or any exception from a rule. In each case:
- the evaluation is recorded as `error` with the exception type, and the assessment as
  `rules_status = error`;
- the rule contributes `max(on_scoring_failure, rule action)`. `on_scoring_failure` comes from
  the policy (`review` by default) and can only be `review` or `decline`.

A rule error therefore **never produces `allow`**. If loading the active rules fails (a database
error), the whole request fails with a 5xx and nothing is persisted, as with any database failure
during scoring. The caller retries with the same `transaction_id`.

**Database invariants** (check constraints on `risk_assessments`):
- `decision` is never less severe than `policy_decision`;
- `rules_status = none` implies `decision = policy_decision`;
- `rules_status = error` implies `decision ∈ {review, decline}`;
- `policy_decision` and `rules_status` must come from their closed sets.

## Versions, activation, audit

* `POST /rules` creates version `max + 1` of a key: version 1 for a new key. The new version is
  inactive. Concurrent creates of one key are serialized by a transaction-scoped advisory lock,
  so they get distinct versions (tested with 8 concurrent creates).
* `rule_definitions` rows are **immutable**. A trigger rejects `UPDATE` and `DELETE`, which is
  tested. Changing a rule means creating a new version.
* The active version of each key is a row in `rule_activations`, with at most one per tenant and
  key (primary key). A composite foreign key `(rule_definition_id, organization_id, rule_key)`
  makes it impossible to activate another tenant's or another key's version.
* Activation and deactivation take a per-tenant advisory lock, upsert or delete the activation
  row, and write the `audit_logs` record **in the same transaction**. A failure leaves neither.
* Concurrent activations therefore serialize. 12 concurrent activations, tested, produce one
  active row and an unbroken audit chain in which each `previous_version` equals the prior
  `version`.
* The optional guard `expected_active_version` (a number or `"none"`) turns a lost update into
  `409 activation_conflict`. Re-activating the active version is a no-op (`changed: false`) and
  writes no audit record.
* A scoring request reads the set of active versions once, inside its own transaction. A score
  never mixes two versions of the same rule. Activations committed while a request is running
  apply to later requests.
* **Audit actions:**

  | Action | Details |
  |---|---|
  | `rule.create` | key, version, action, priority, `condition_sha256` |
  | `rule.activate`, `rule.deactivate` | key, version, `previous_version` |
  | `fraud.score` | `policy_decision`, `rules_status`, `rules_evaluated`, `rules_matched`, `decided_by` |

**Idempotent retries.** A retry with the same `transaction_id` and payload returns the stored
assessment: the original decision and the rule versions and outcomes stored with it. This holds
even if rules were added, changed or deactivated since (tested). `rule_evaluations` is append-only
(trigger).

## Roles

| | admin | analyst | viewer | service |
|---|---|---|---|---|
| list / inspect rules, grammar | ✓ | ✓ | ✓ | – |
| create a rule version | ✓ | ✓ | – | – |
| activate / deactivate | ✓ | – | – | – |
| analytics endpoints | ✓ | ✓ | ✓ | – |

Analysts can draft rules but cannot put them into effect. Every rule endpoint is scoped to the
caller's organization: another tenant's key returns `404`, as if it did not exist.

## Analytics definitions

All endpoints take:
- `start` and `end`: ISO 8601 **with an offset**, converted to UTC, half-open `[start, end)`,
  at most 366 days;
- `basis`: `processed` (default) or `event`;
- `source`: repeatable; `live`, `replay`, `simulated` or `backfill`.

| Basis | Transactions are windowed by | Assessments are windowed by |
|---|---|---|
| `processed` | `received_at` | `scored_at` |
| `event` | `event_time` | the transaction's `event_time` |

Every number is an aggregate PostgreSQL computes from stored rows at request time. Each rate
returns `{numerator, denominator, rate}`, and `rate` is `null` when the denominator is 0.

| Endpoint | Metric | Definition |
|---|---|---|
| `overview` | `transactions.received / scored / unscored` | transactions in the window; those with an assessment; imported history without one (`backfill` rows are never scored) |
| | `assessments.by_*` | counts by status, final decision, policy decision, risk level, source, rules status (every category listed, zeros included) |
| | `alert_rate` | `decision ≠ allow` / assessments |
| | `policy_alert_rate` | `policy_decision ≠ allow` / assessments |
| | `rule_escalation_rate` | `decision ≠ policy_decision` / assessments |
| | `degraded_rate` | `status = degraded` / assessments |
| `volume` | per bucket: transactions, assessments, alerts, decisions | `bucket=hour|day`; `start`/`end` must be on UTC bucket boundaries; ≤ 1000 buckets; empty buckets returned as zeros |
| `rule-hits` | per rule version: evaluated, matched, unknown, errors, influenced, `match_rate = matched/evaluated` | over `rule_evaluations` of assessments in the window; plus assessments, with rules, with ≥ 1 match, escalated |
| `latency` | count, mean, p50/p90/p95/p99 (`percentile_cont`, linear interpolation), max | `timings_ms.total`: server-side scoring time from validation to decision, **excluding the commit, network and stream queueing**; components `features`, `inference`, `rules` over the rows that recorded them |

Queue lag (ingest to commit) of the stream path is not persisted, so it is not available here. It
is measured by the Phase 5 harness (see [streaming.md](streaming.md)).

## Verification

Results as of 2026-10-27:
- **Static checks:** `ruff` clean, `mypy --strict` clean. Migration `0003` upgrade → `alembic
  check` (no drift) → downgrade to `0002` → upgrade → downgrade to base → upgrade, all in a
  throwaway database.
- **Unit tests:** 42 cover the grammar, three-valued logic, precedence and failure handling.
- **Integration tests:** 34 rules and analytics tests against PostgreSQL 17.6 and Redis 7.4.2,
  covering validation, immutability triggers, versioning races, activation races and the
  compare-and-set guard, audit, roles, tenant isolation, no-rules behaviour, escalation without
  score changes, idempotent retries after rule changes, compile and runtime failures, database
  invariants, degraded scores, single/batch/stream parity, the migration backfill, and analytics
  against hand-built fixtures (empty windows, tenant and source filters, both bases, buckets,
  percentiles, invalid windows).

**Real-data check.** 5,000 held-out events (from 2020-06-21T12:14Z) were sent to four tenants in
a throwaway database. Redis DB 14 was used, and both were removed afterwards. Tenant A used batch
with no rules. Tenants B (batch), C (stream, one worker) and D (single requests) each had the same
6 illustrative rules:
- risk score ≥ 0.5 → decline;
- online and ≥ $500 → review;
- ≥ 3 transactions in the past hour → challenge;
- night and ≥ $300 → review;
- amount z-score ≥ 3 → review;
- grocery → flag.

| Check | Result |
|---|---|
| Model side (`risk_score`, features, `policy_decision`, level) A = B = C = D | identical for 5,000 / 5,000 |
| Rule side (decision, rules status, reasons, per-rule outcomes) B = C = D | identical for 5,000 / 5,000 |
| Tenant A without rules | `decision = policy_decision` for all 5,000, `rules_status = none` |
| Never relaxed | true for all 5,000 |
| Escalated by rules (B) | 1,093 assessments |
| Rule outcomes (B), e.g. amount z-score rule | 384 matched, 2,937 not matched, 1,679 unknown (no 30-day history in this database) |
| Analytics vs pandas over the same rows | total, decision mixes, alert and escalation counts, per-rule hits, latency p50/p99: all equal |
| Rule step, `timings_ms.rules`, 6 active rules | p50 0.52 ms, p95 1.27 ms, p99 1.71 ms |
| Rule step with 0 active rules (lookup only) | p50 0.55 ms, p95 1.02 ms, p99 1.53 ms |

The rule step's cost is dominated by the per-request lookup of active rules, not by evaluation.
The decision mix in this run is **not representative**: the throwaway database held no history
from before the window, so velocity and 30-day features start cold, and the rules are examples,
not tuned policies.

## Limitations

* Rules can only escalate, so false positives from the model cannot be suppressed by a rule.
* There are no rule simulations or backtests yet: a new version cannot be dry-run against stored
  assessments before activation.
* The active-rule lookup is one indexed query per score. There is no cache; that keeps activation
  immediate and consistent across API instances and workers.
* Activation is per rule key. Several keys cannot be switched in one atomic change set; each key
  switches atomically on its own.
* `rule_evaluations` stores one row per evaluated rule per assessment, including non-matches.
  With 100 active rules that is 100 rows per transaction. A retention job and monthly
  partitioning, both designed in database-design.md, would be needed at volume.
* Analytics are computed on request over raw rows. A 366-day window on a large tenant would need
  pre-aggregated rollups, which are not built. There is no per-endpoint rate limit (Phase 10).
* The feed (`/stream/assessments`) carries `policy_decision` in addition to `decision`, but not
  the rule trace. The trace is available from `GET /fraud/predictions/{id}`.
