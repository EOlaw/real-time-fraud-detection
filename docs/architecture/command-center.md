# Command center (Phase 7)

`apps/web` is a Next.js 15 app (App Router; React 19; TypeScript strict; no UI or chart
libraries; SVG charts). It builds on the dashboard that existed before Phase 7: the portfolio,
dashboard, diagnostics, settings and sign-in pages are kept and extended, and analytics, live
and rules pages are added. Phase 8 adds the case queue and case detail pages (below).

## Data flow and trust boundaries

* The browser talks only to its own origin. `next.config.ts` rewrites `/api/v1/*` and `/health/*`
  to `FRAUD_API_URL`, so no CORS is needed and the token never goes to another origin. The rewrite
  target is fixed at build time for `next start`.
* The token is kept in `sessionStorage` (per tab, cleared on tab close) and dropped at its expiry
  time. The role claim is read only to hide controls (`lib/auth.tsx` `CAN`). **The API enforces
  every permission**; the UI says so on the Rules and Settings pages.
* A 401 from any request ends the session and shows the reason on the sign-in form. Service
  accounts get an explanation instead of pages that would all fail with 403.
* Every number comes from an API response:

  | Data | Source |
  |---|---|
  | Lists | `GET /fraud/assessments`, which since Phase 7 also returns `policy_decision`, `rules_status` and a repeatable `source` filter |
  | Details | `GET /fraud/predictions/{id}` with `rule_evaluations` |
  | Explanations | `…/explanation` |
  | Charts | `/fraud/insights/*` |
  | Rules | `/rules*` |
  | Live updates | `/stream/assessments` |

## Decision display rules

* **Model probability** is shown as the model's value, labelled "rules never change this value".
  The probability ring and gauge use the **policy** band colour, not the final decision's colour.
* **Policy decision** (model-bound) and **final decision** are shown separately. When they
  differ, tables show `Review → Decline · rule escalated`, and the trace names the deciding rule
  version (`Escalated from Review by rule model_high@v1`). Rule errors show the fail-safe
  escalation.
* The **rule trace** lists every evaluated version with priority, action, outcome (matched,
  not matched, unknown (missing value), error) and whether it decided. It is the trace stored at
  scoring time; later rule changes do not alter it.
* **Explanations** have explicit states: degraded (no score to explain), model mismatch (scored by
  a non-serving model version, so it cannot be reproduced), and API error.
* **Anomaly panel:** offline-only baseline, not served and not used in decisions.

## Live feed client (`lib/feed.ts`, `lib/sse.ts`)

* `fetch` with `Authorization` and `Accept: text/event-stream`, parsed by an incremental SSE
  parser. The parser handles CR, LF and CRLF line endings, including a CRLF split across chunks,
  as well as comments, `id`, `retry` and multi-line `data`.
* **Order on first load:** connect first, then load the latest 50 persisted assessments, so
  nothing committed between the two is missed. Overlaps are removed by `assessment_id`.
* **Reconnects:** after a server close, wait `retry` (3 s) and reconnect with `Last-Event-ID`.
  After failures, back off exponentially up to 30 s, with a visible countdown and the error text.
* **Watchdog:** if no bytes (event or heartbeat) arrive for 45 s, three missed heartbeats, the
  connection is treated as dead and reconnected. This is needed because a proxy can keep the
  browser's connection open after the API process has died (observed below).
* **Duplicates:** at-least-once delivery means repeated `assessment_id`s are counted and dropped.
* **`reset` event:** shows a notice and reloads persisted assessments.
* **Error codes:** 401 ends the session ("expired while the live feed was open"), 403 is shown,
  and 503 or other failures are retried.
* Pause/resume and "Reconnect now" controls. The counters shown are events received, duplicates
  dropped, connections, resets, last event, last heartbeat and the current cursor.

**Required response header.** Next.js gzip-compresses proxied responses. For an event stream,
compression buffers events until a block fills: in the browser check, nothing arrived for 8 s.
The SSE endpoint therefore sends `Cache-Control: no-cache, no-transform`, and the compression
layer skips responses marked `no-transform`. This is a one-line Phase 7 change to the Phase 5
route, covered by `test_sse_response_must_not_be_compressed_by_proxies`.

## Accessibility and layout

* Skip link, labelled navigation, `aria-current`, visible focus rings, and `aria-sort` on
  sortable headers. Rows are selected with a button inside the row.
* Charts are SVG with `role="img"` and a "Data table" fallback. Bar lists are real tables with
  numbers.
* Status changes use `aria-live` and `role="status"`/`alert`. Scrollable regions are focusable and
  labelled.
* One contrast fix to the existing palette: the `Review` badge changed from white on `#b96c2b`
  (3.6:1, which failed AA) to white on `#94531c` (5.5:1).
* At 390 px wide, pages do not scroll horizontally; wide tables scroll inside their own region.

## Verification (2026-10-27)

**Static checks:**
- `npm run typecheck`: no errors;
- `npm run build`: success, 10 routes;
- SSE parser: standalone Node check at every chunk size from 1 to 25.

**Backend:** 278 passed, 0 failed, 0 skipped, including the 3 new API-contract tests.

**Browser checks** ran in headless Google Chrome via `playwright-core` (installed outside the
repository), against `next start` built for a real local API with PostgreSQL 17.6 and Redis 7.4.2.
They used a throwaway database and Redis DB 13, both removed afterwards. Seed data:
- 19,970 backfill rows;
- 1,500 replayed held-out events through `/fraud/batch`;
- 20 `live`-labelled singles;
- 5 degraded scores from an API instance started without a model;
- 5 active rules plus drafts;
- simulator traffic through the stream worker;
- one clearly labelled fixture row (`fixture_model_mismatch`) referencing the registered,
  non-serving `1.1.0` candidate. Registering never activates it.

| Check | Result |
|---|---|
| Rule-influenced decision in table and trace | `Review → Decline · rule escalated`; trace `Escalated from Review by rule model_high@v1`, probability 0.9985 shown unchanged; matches the database row (`review → decline`) |
| Degraded and model-mismatch states | "None: scoring degraded", explanation "Degraded assessment…"; model-mismatch notice shown |
| Source filter (dashboard) | `live` shows 20 of 20, database has 20; badges shown on every row |
| Analytics vs SQL over the same window | 13/13 equal: transactions, assessments, alert / policy-alert / escalation / degraded counts, both decision mixes, per-rule matches, latency p50 and p99, event-time basis received and unscored counts |
| Empty window, no source selected | Empty states and `n/a` rates with `0 / 0`; "Select at least one source." |
| Live update | 30 simulated stream events appeared live; cursor shown |
| Duplicates | Re-ingesting the same 30 events: 30 duplicates dropped, still 30 rows (database 30) |
| Manual reconnect | Connections 2, 10 further events received, no gap |
| API crash (`kill -9`) | The Next.js proxy kept the browser connection open; watchdog reported "No events or heartbeats for 45 s" and retried; 10 events scored through another API instance meanwhile; after restart, reconnected with `Last-Event-ID` and received all 10 |
| `reset` | Feed trimmed past the cursor while paused; on resume: reset notice, 2 events via the feed and 4 reloaded from the database, all 6 shown |
| Empty feed, other tenant | Connected, 0 rows, "No assessments yet" (no cross-tenant data) |
| Server 401 | Invalid token: session ended with "expired while the live feed was open" |
| Client expiry | Clock fast-forwarded past the token lifetime: "Your session expired." |
| Idle heartbeat | Received ("Last heartbeat 0s ago") |
| Roles (Rules page) | Viewer: no create or activate controls. Analyst: create only. Admin: create, activate, deactivate. Service: explanation page |
| Rule validation | `$.field: unknown field`; nothing stored |
| Admin activation | "Active version: 1 (was none). Audited."; `rule.create` and `rule.activate` audit rows present |
| Stale tab | Compare-and-set guard: "Active version is none, not 1 (activation_conflict)" |
| Keyboard | Skip link → brand → 7 nav links → search → source filter → sort headers → row buttons, all with focus rings except the search input, which highlights its container; Enter selects a row |
| axe-core 4.10.3, WCAG 2.1 A/AA | 0 violations on 7 pages × desktop and phone, after fixing the issues it found (contrast, focusable scroll regions, link distinguishable) |
| 390 px phone width | No page-level horizontal overflow on dashboard, analytics, live, rules or portfolio |

Screenshots of each state were saved outside the repository during the run; they are evidence for
the review, not committed assets.

## Cases and labels (Phase 8)

* **Opening a case.** The Dashboard's decision-trace panel has an **Open case** button for
  analysts and admins. It opens the case for that transaction, or the existing one, and navigates
  to it.
* **Case queue (`/cases`).** Filters for open/closed/all, assignment (anyone, me, unassigned) and
  severity, with sorting and paging. Each row shows severity, status, assignee, model
  probability, the policy and final decision with any rule escalation, the current label, and the
  source label.
* **Case detail (`/cases/[id]`):**
  - transaction facts: token, merchant, amount, category, channel, time and source;
  - the scoring model version, flagged when this API instance doesn't serve it;
  - the stored decision trace with rule hits, and the SHAP explanation with its degraded and
    model-mismatch states;
  - notes, which are append-only;
  - status and assignment history;
  - the label chain with provenance: source, confidence, when the outcome was known vs recorded,
    who recorded it, and the reason for each correction. Superseded labels stay visible.
* **Action controls follow the policy:** claim and release, admin assignment, legal status moves,
  resolve (optionally recording a label), notes, and label recording or correction. Viewers see
  everything read-only. Every write sends the version the page read. A `409` shows "The case
  changed since you read it… nothing was changed", and the page reloads the latest data.
* **Analytics** gain an "Investigations and labels" section:
  - case counts and resolution outcomes;
  - label counts by source and confidence;
  - **label-matured performance** with as-of, maturity and include-probable controls, the
    exclusion counts and the selection-bias caveat;
  - a separate **unlabeled signals** card.

### Phase 8 browser verification (2026-10-27)

Headless Chrome via `playwright-core`, against `next start` and a real local API, PostgreSQL 17.6
and Redis 7.4.2. They used a throwaway database and Redis DB 12, both removed afterwards. Seed:
backfill history, 5 rules, 1,500 replayed events, 3 degraded scores, and dataset labels for the
replayed window.

| Check | Result |
|---|---|
| Open case from a rule-escalated transaction | Navigated to the case. Trace "Escalated from Challenge by rule …", 6 SHAP bars, dataset label shown as current, history "opened the case" |
| Work and resolve | Claim (v2), under review (v3), note, then resolve with `record_label`: refused with `label_exists` (dataset label present), still v3. Resolved legitimate (v4) |
| Label correction | Analyst-review label superseded the dataset label; both shown, the old one struck through as "superseded by #…" |
| Decision untouched | `risk_score / policy_decision / decision` identical before and after |
| Audit | `case.create, case.assign, case.status, case.note, case.resolve, label.supersede` |
| Stale tab | An admin assigned analyst2. The analyst's stale "Assign to me" showed the conflict message; the page reloaded to v2 and the database kept analyst2 |
| Viewer | Queue and case readable; no buttons, no note or label forms |
| Degraded assessment | Severity high, "None: scoring degraded", model "none (degraded)", "no model score to explain" |
| Duplicate open | `200`, `created: false`, same case |
| Cross-tenant case id | "Case unavailable: Case not found" |
| Analytics vs SQL | Cases opened and resolved, usable labels (1,523: 0 fraud / 1,523 legitimate), FP and FN counts, and label sources (analyst review 3, dataset 1,520) all equal |
| axe-core, WCAG 2.1 A/AA | 0 violations: 8 pages at desktop and phone width, plus case detail at both widths |
| 390 px phone width | No page-level horizontal overflow on `/cases`, `/cases/[id]`, `/analytics` (fixed during the check: grid columns and a labels table) |

The replayed window contains no `is_fraud = 1` events, so the browser run shows 0 true positives.
The confusion-matrix arithmetic, maturity cutoffs, point-in-time labels and probable or
inconclusive exclusion are verified against a hand-built fixture in
`tests/integration/test_labels.py`.

## Model monitoring panel (Phase 9)

The **Diagnostics** page has a **Model monitoring** section (`components/MonitoringPanel.tsx`). It
reads `GET /api/v1/monitoring/runs` and `/monitoring/runs/{id}` and shows:
- **Run selector and header:** windows of the caller's organization, newest first. The header
  shows status, recommendation (marked as acknowledged when it is), assessment count, model, the
  reference window with its event count, and the calculation version.
- **What the statuses mean:** a fixed note that drift never promotes or retrains a model, plus
  the PSI thresholds and the minimum sample.
- **Two separate tables:**
  - unlabeled distribution and operational signals;
  - label-matured performance on the labeled subset, with the selection-bias caveat directly
    above it.

  Each row shows the value, the 95% interval where one exists, current/reference sample sizes and
  the status.

The panel is read-only: no button runs monitoring, acknowledges, promotes or retrains.
Readers (admin, analyst, viewer) see it; service accounts cannot sign in to the UI.

### Phase 9 browser verification (2026-10-28)

**Setup:**
- Headless Chrome via `playwright-core`, against `next start` built for a throwaway API.
- PostgreSQL 17.6 database `fraud_phase9_check` and Redis DB 11, both removed afterwards.
- A run over 20,000 batch-scored held-out events, with dataset labels (68 fraud).

| Check | Result |
|---|---|
| Header | "Alert · recommendation: retraining review (acknowledged) · 20000 assessments by card-fraud-xgb@1.0.0 · reference: validation window 2020-03-01 to 2020-06-21 (271,832 events) · drift-v1" |
| Tables | 21 unlabeled rows, 7 labeled rows (e.g. `precision_final` 42.58%, 95% interval 0.3507–0.5045), caveat shown |
| Tenant isolation | A user of the other tenant saw only that tenant's own run (`insufficient_data`) |
| axe-core, WCAG 2.1 A/AA | 0 violations at desktop and phone width |
| 390 px phone width | No page-level horizontal overflow |

## Limitations

* The dashboard list does not auto-refresh; the Live page carries real-time updates.
* Case pages don't poll for changes made in other tabs; staleness is caught by the version check
  on the next write.
* No frontend unit-test runner or end-to-end suite in the repository. The browser checks above
  were scripted for this review and are not part of CI.
* The token is a bearer token in `sessionStorage`, readable by any script on the origin, so an XSS
  would expose it. httpOnly cookies with CSRF protection would be the hardening step (Phase 10).
* `FRAUD_API_URL` is fixed at build time for `next start`.
* Uvicorn's graceful shutdown waits for open SSE connections, which can last up to
  `FRAUD_SSE_MAX_CONNECTION_SECONDS` = 30 min. Since Phase 9 the API image sets
  `--timeout-graceful-shutdown 10`; `make api` (local dev) does not. The client reconnects either
  way.
* The live feed keeps the newest 300 items in memory and does not page further back; use the
  dashboard list for history.
* Automated accessibility checks do not cover everything; no screen-reader session was run.
