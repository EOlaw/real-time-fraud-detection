# Rules and analytics API (Phase 6)

Bearer token required on every endpoint. Errors use the standard envelope
`{"error": {"code", "message", "correlation_id", "details"?}}`. Semantics (grammar, precedence,
fail-safe, metric definitions) are in
[docs/architecture/rules-engine.md](../architecture/rules-engine.md); this page is the wire
contract.

| Method | Path | Roles |
|---|---|---|
| GET | `/api/v1/rules/grammar` | admin, analyst, viewer |
| GET | `/api/v1/rules` | admin, analyst, viewer |
| POST | `/api/v1/rules` | admin, analyst |
| GET | `/api/v1/rules/{rule_key}` | admin, analyst, viewer |
| GET | `/api/v1/rules/{rule_key}/versions/{version}` | admin, analyst, viewer |
| POST | `/api/v1/rules/{rule_key}/versions/{version}/activate` | admin |
| POST | `/api/v1/rules/{rule_key}/deactivate` | admin |
| GET | `/api/v1/fraud/insights/overview` | admin, analyst, viewer |
| GET | `/api/v1/fraud/insights/volume` | admin, analyst, viewer |
| GET | `/api/v1/fraud/insights/rule-hits` | admin, analyst, viewer |
| GET | `/api/v1/fraud/insights/latency` | admin, analyst, viewer |

`service` tokens are refused (`403`) on all of them. Rules of another organization are reported
as `404`.

## Rules

### `POST /api/v1/rules` → `201 RuleVersionOut`

```json
{
  "rule_key": "large_night_cnp",
  "name": "Large card-not-present payment at night",
  "description": "optional, ≤ 1000 chars",
  "priority": 500,
  "action": "review",
  "condition": {"all": [
    {"field": "channel", "op": "eq", "value": "card_not_present"},
    {"field": "amount", "op": "gte", "value": 900},
    {"field": "feature.is_night", "op": "eq", "value": 1}
  ]}
}
```

* `rule_key`: `^[a-z0-9][a-z0-9_.-]{0,63}$`. `priority`: 0–1000 (default 100; higher first).
  `action`: `flag|challenge|review|decline` (`allow` is not accepted). Unknown body fields → 422.
* Creates the next version of the key (1 for a new key), **inactive**.
* `422 invalid_rule` when the condition violates the grammar; `details` is a list of
  `{"path": "$.all[1].op", "message": "operator not allowed for decimal field"}`.

`RuleVersionOut`: `id` (the version's id), `rule_key`, `version`, `name`, `description`,
`priority`, `action`, `condition`, `condition_sha256` (canonical JSON), `created_by`,
`created_at`, `active`.

### `GET /api/v1/rules` → `RuleListOut`

`{"active_count", "max_active": 100, "items": [{"rule_key", "latest_version", "versions",
"active_version" | null, "active": RuleVersionOut | null, "activated_at" | null}]}`, ordered by key.

### `GET /api/v1/rules/{rule_key}` → `RuleDetailOut`

`{"rule_key", "active_version", "activated_at", "versions": [RuleVersionOut, …]}`, newest
first. `GET …/versions/{version}` returns one `RuleVersionOut`. Missing → `404 not_found`.

### `POST /api/v1/rules/{rule_key}/versions/{version}/activate` → `ActivationOut`

Optional body: `{"expected_active_version": 3}` or `{"expected_active_version": "none"}`
(compare-and-set guard). Response:
`{"rule_key", "active_version", "previous_version", "changed", "activated_at"}`.

| Status | Meaning |
|---|---|
| 200 `changed: true` | switched; `rule.activate` audit record written in the same transaction |
| 200 `changed: false` | already active; nothing written |
| 404 `not_found` | no such version in the caller's organization |
| 409 `activation_conflict` | the guard did not match the current active version |
| 409 `too_many_active_rules` | activating a new key would exceed 100 active rules |

### `POST /api/v1/rules/{rule_key}/deactivate` → `ActivationOut`

Same guard and status codes. `changed: false` if nothing was active. Writes `rule.deactivate`.

## Changes to the transaction list (Phase 7)

`GET /api/v1/fraud/assessments` rows also carry `policy_decision` and `rules_status`, and the
endpoint accepts a repeatable `source` filter (`live|replay|simulated|backfill`; unknown → 422).
These are additive changes made for the command center.

## Changes to scoring responses

`ScoreResponse` (from `/fraud/score`, inside `/fraud/batch` items, and on idempotent replays) now
also carries:

```json
{
  "decision": "decline",
  "policy_decision": "allow",
  "rules": {
    "status": "evaluated",
    "evaluated": 3,
    "matched": [{"rule_id": "…", "rule_key": "large", "version": 1, "action": "decline"},
                {"rule_id": "…", "rule_key": "net_flag", "version": 1, "action": "flag"}],
    "errors": [],
    "decided_by": {"rule_id": "…", "rule_key": "large", "version": 1, "action": "decline"}
  },
  "reasons": ["score<0.0122", "rule large@v1: decline", "rule net_flag@v1: flag"]
}
```

* `decision` is the final action, and `policy_decision` is the model-bound policy's decision.
  With no active rules, `rules.status` is `"none"` and the two are equal.
* `GET /api/v1/fraud/predictions/{id}` adds `rule_evaluations`: every evaluated version with
  `priority`, `outcome` (`matched|not_matched|unknown|error`), `influenced_decision` and
  `error_type`.
* The SSE feed adds `policy_decision`. These are additive fields; nothing was removed or renamed.

## Analytics

Common query parameters:

| Parameter | Required | Meaning |
|---|---|---|
| `start` | yes | inclusive, ISO 8601 with offset (`2026-10-01T00:00:00Z`) |
| `end` | yes | exclusive; `end > start`; at most 366 days after `start` |
| `basis` | no | `processed` (default: `received_at` / `scored_at`) or `event` (`event_time`) |
| `source` | no, repeatable | `live`, `replay`, `simulated`, `backfill`; default all |

`400 invalid_window` covers:
- naive datetimes, `end ≤ start`, or windows longer than 366 days;
- for `volume`, windows not aligned to the bucket or longer than 1000 buckets.

An unknown `source` → `422`. Every response echoes `window`
(`{start, end, basis, sources}` in UTC). Rates are `{numerator, denominator, rate}`, and `rate` is
`null` when the denominator is 0.

**`GET /fraud/insights/overview`**:
- `transactions {received, scored, unscored}`;
- `assessments {total, by_status, by_decision, by_policy_decision, by_risk_level, by_source,
  by_rules_status}`;
- rates: `alert_rate`, `policy_alert_rate`, `rule_escalation_rate`, `degraded_rate`.

**`GET /fraud/insights/volume?bucket=hour|day`**: `buckets: [{start, transactions, assessments,
alerts, by_decision}]`, one per UTC bucket, with zeros included.

**`GET /fraud/insights/rule-hits`**:
- assessment counts: `assessments`, `assessments_with_rules`, `assessments_with_match`,
  `assessments_escalated`;
- `rules: [{rule_id, rule_key, version, action, evaluated, matched, unknown, errors, influenced,
  match_rate}]`, ordered by matches descending.

**`GET /fraud/insights/latency`**:
- `processing_ms {count, mean, p50, p90, p95, p99, max}` from `timings_ms.total`;
- `components_ms.features|inference|rules` in the same shape. Percentiles are `percentile_cont`;
  everything is `null` for an empty window.

### Example

```bash
TOKEN=...   # an analyst or viewer token
curl -s -G http://127.0.0.1:8000/api/v1/fraud/insights/overview \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode start=2026-10-27T00:00:00Z --data-urlencode end=2026-10-28T00:00:00Z \
  --data-urlencode source=replay --data-urlencode source=simulated
```
