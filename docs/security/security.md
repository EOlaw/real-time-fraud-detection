# Security controls

No regulatory compliance (PCI DSS, SOC 2, GDPR, …) is claimed. This page lists what is implemented
and tested, and what a regulated deployment would additionally require.

## Implemented

| Control | Implementation | Test evidence |
|---|---|---|
| Authentication | JWT HS256, algorithm pinned (no `none`, no alg confusion), `iss`/`aud`/`exp`/`iat` required, 30-min TTL | `test_forged_token_rejected`, `test_unsigned_alg_none_token_rejected` |
| Passwords | argon2id; min length 12; constant-ish timing for unknown users | `test_bad_login` |
| Authorization | Roles `admin`, `analyst`, `viewer`, `service`; scoring = service/admin only | `test_read_only_roles_cannot_score` |
| Tenant isolation | `organization_id` from token on every query; foreign tenant ids return 404 | `test_cross_organization_isolation` |
| Card data minimization | PAN replaced by keyed HMAC token at ingestion; names, address, DOB, gender, job never loaded (CSV `usecols`, Parquet column projection); API rejects unknown fields (e.g. `cvv`) | `test_success_writes_outputs_and_complete_report`, `test_validation_errors[cvv]` |
| No sensitive values in pipeline outputs | Quality reports show values only for non-sensitive columns (`[redacted]` otherwise); parser errors record exception type only; import errors print only the first DB error line | `test_sensitive_raw_values_never_appear_in_outputs_reports_or_logs` (success, schema failure, conflict: files, decoded parquet, stdout/stderr, logs), `test_unreadable_inputs_fail_without_echoing_content`, `test_database_rows_contain_no_sensitive_raw_values` |
| Data import integrity | Manifest hash check before import; tenant-scoped, idempotent, conflict-rejecting, single-transaction import; `live` source refused for files | `tests/integration/test_import.py` |
| Error hygiene | Validation errors return locations/messages but not submitted values; unhandled errors return a generic 500 | `test_validation_errors` |
| Log redaction | Keys such as `password`, `authorization`, `token`, `customer_id`, `database_url` are redacted; request bodies are never logged | code review (`core/logging.py`) |
| Audit trail | Login (success/failure), user creation, every import and every score are audited; DB trigger makes `audit_logs` append-only | `test_scoring_is_audited_and_audit_log_is_append_only` |
| Safe model loading | XGBoost JSON only (no pickle/joblib); SHA-256 verified against manifest and the DB registry; path traversal blocked; feature contract checked | `test_tampered_model_file_is_rejected`, `test_model_file_outside_artifact_dir_is_rejected`, `test_reusing_model_version_with_different_bytes_is_refused` |
| Secrets | Only from environment/`.env` (git-ignored); weak or placeholder JWT secret refused at startup; no credentials in code or compose file | `Settings` validator |
| Container | Non-root user (uid 10001); model mounted read-only; ports bound to 127.0.0.1 locally | verified with `docker compose exec api id` |
| Sandbox decisions | Policy schema only accepts `mode: sandbox`; no money movement integration | `test_only_sandbox_mode_is_supported` |
| Experiment tracking hygiene | MLflow receives only config, versions, hashes and aggregate metrics; `tracking.guard` rejects customer tokens, 32-hex event ids and secret env values; per-transaction explanations stay in the git-ignored artifact dir | `test_tracker_records_contain_no_transaction_data_or_secrets`, `test_guard_rejects_sensitive_values`, `test_mlflow_tracking_end_to_end` (scans the on-disk store) |
| Explanation output | Per-transaction SHAP shows model inputs only; identifier columns are refused | `test_explanations_refuse_identifiers` |
| Anomaly score isolation | Unsupervised score is a separate type; the decision policy only accepts a probability float; never stored in model artifacts or policies | `test_anomaly_score_cannot_reach_the_decision_policy_or_model` |
| Batch scoring | Same roles, tenant scoping and validation as single scoring; per-item validation errors never echo submitted values; failed items carry no decision; per-item and per-batch audit records; item-count limit (413) | `test_mixed_batch_outcomes_are_explicit_per_item`, `test_tenant_isolation`, `test_read_only_roles_cannot_batch_score`, `test_batch_envelope_limits` |
| Model metadata | Authenticated, read-only; built from an allowlist of manifest fields (no filesystem paths, training config, artifact file names, data or secrets) | `test_model_metadata_exposes_no_paths_secrets_or_training_config`, `test_model_endpoints_require_auth_and_return_404` |
| Streaming ingest and feed | Tenant taken from the token (body cannot name an organization); per-item validation without echoing values; SSE feed key derived from the token's organization; feed payload is an allowlist (no amount, customer, merchant, region, features); stream status admin-only; DLQ reasons carry fixed text and error *types* only | `test_ingest_validates_labels_and_shards_with_tenant_from_token`, `test_feed_is_tenant_scoped`, `test_end_to_end_scoring_feed_and_clean_streams`, `test_poison_messages_are_dead_lettered_immediately`, `test_retry_exhaustion_goes_to_dlq_and_replay_scores_it` |
| Redis | Password-protected, bound to 127.0.0.1:6380, `noeviction`; tests use DB 15 with per-test prefixes and never inherit the developer's `FRAUD_REDIS_URL` | `test_streaming_does_not_touch_other_redis_databases` |
| Rules engine | Conditions are data in a closed grammar (allowlisted fields, typed literals, bounded size/depth) compiled to a fixed interpreter, never `eval`'d; validation errors return paths, not values; rule versions and evaluations append-only (trigger); analysts create, only admins activate, viewers read, service tokens have no access; activation and its audit record commit together; composite FKs keep activations/evaluations inside the tenant; rules can only escalate and errors escalate (DB check constraints) | `test_invalid_conditions_are_rejected`, `test_errors_never_echo_submitted_values`, `test_rule_versions_and_evaluations_are_immutable_in_the_database`, `test_role_restrictions`, `test_tenant_isolation` (rules), `test_activation_switching_and_audit`, `test_database_rejects_lenient_or_relaxed_decisions` |
| Analytics | Tenant from the token on every aggregate; bounded windows (≤ 366 days, ≤ 1000 buckets); readers only (service tokens refused); aggregates only, no per-transaction values | `test_empty_window_and_tenant_isolation`, `test_authorization`, `test_invalid_windows` |
| Command center (web) | Same-origin proxy (no CORS, token never sent cross-origin); token in `sessionStorage`, dropped at expiry; a 401 ends the session; role-based hiding of controls with the API as the enforcement point; no amounts, customers or features on the live feed; service accounts get no analyst views | Phase 7 browser checks in `docs/architecture/command-center.md` (roles, 401, expiry, tenant isolation) |
| Cases and labels | Tenant from the token; foreign ids are 404 (assignees 422); analysts work only their assigned cases, admins assign and reopen, viewers read, service accounts have no access; optimistic locking (409) plus a DB trigger requiring version +1; notes, case events and labels append-only (triggers); every create, assignment, note, transition and label in `audit_logs` in the same transaction; labeled_by from the token; no PAN, region or raw payload in case responses; cases and labels never write to `risk_assessments`; DB failures return 503 with nothing changed | `tests/integration/test_cases.py`, `tests/integration/test_labels.py` (RBAC, tenant isolation, stale updates, concurrent transitions and corrections, append-only triggers, audit trail, decision unchanged, database failure) |
| Gated promotion | Candidate models are never auto-promoted. Phase 9: `fraud_api.monitoring.cli promote --apply --operator` runs every gate (artifact hash/contract, policy binding, threshold provenance, parity, feature contract, reproducibility, reference, minimum samples, drift, labeled performance, unresolved monitoring alerts); any failure or exception refuses (fail closed, with reasons). It archives the previous policy and writes a `model.promote` audit record with operator and gate results; `rollback` writes `model.rollback`. The legacy `promote-model --apply` (artifact gates only) now refuses | `test_gates_fail_closed_with_reasons`, `test_gates_pass_promote_and_rollback_are_audited`, `test_legacy_promote_apply_is_refused`, plus the Phase 3 gate tests |
| Metrics endpoint | `/metrics` off (404) unless `FRAUD_METRICS_TOKEN` (≥ 24 chars) is set; then bearer token compared in constant time, 401 otherwise; separate from user JWTs and from `/health/*`; not in OpenAPI. Labels are bounded: route templates and closed value sets only, anything else `other`/`unmatched`; no transaction, customer, merchant, case, tenant or user ids, tokens, URLs or free text. Worker metrics bind to 127.0.0.1 only. Prometheus reads the token from a git-ignored, mode-600 file | `test_metrics_endpoint_access_control`, `test_metrics_reflect_scoring_without_identifiers`, cardinality unit tests in `tests/unit/test_monitoring_unit.py` |
| Model monitoring | Monitoring reads stored feature snapshots and aggregates; it never loads raw payloads and logs aggregates only. Reference profiles hold bins/quantiles, never rows. Runs per tenant are visible only to that tenant (foreign or global run ids are 404); global (all-tenant) runs are operator-only via CLI. Monitoring cannot change scores, decisions, cases, labels or the serving model; references append-only, runs and metrics undeletable (triggers); runs and acknowledgements audited | `test_runs_are_idempotent_tenant_scoped_and_detect_known_drift` (incl. a snapshot proving nothing else changed), `test_reference_is_versioned_immutable_and_checked` |
| Database overload | Pool timeouts and operational DB errors return `503 database_unavailable` with `Retry-After`, not a 500 with internals | `test_database_overload_is_503_with_retry_after_and_counted` |
| Load tool | Authenticates as its own tenant only; password from env, never argv; proven not to change another tenant's data or the model state | `test_load_tool_only_adds_its_own_tenant_data` |

## Not yet implemented (required before any real deployment)

* API rate limiting and login lockout (Phase 10, Redis-backed).
* Web session hardening: an httpOnly cookie session with CSRF protection instead of a bearer
  token in `sessionStorage` (readable by scripts on the origin), plus a Content-Security-Policy.
* Case data retention and redaction policy (notes are free text and append-only, so a note
  containing sensitive data cannot be edited; it would need an audited redaction mechanism).
* Four-eyes approval for rule activation (today a single admin can activate; every activation is
  audited with actor and previous version). Rule backtesting before activation.
* Redis TLS/ACL users per role, and encryption or restricted access for the dead-letter stream, which holds original payloads.
* TLS termination (ALB/ACM in AWS) and HSTS; currently plain HTTP on localhost.
* Asymmetric JWT (RS256/EdDSA via an identity provider) with key rotation; token revocation.
* Secrets manager (AWS Secrets Manager) instead of `.env`; KMS-managed tokenization key with rotation
  strategy (rotation changes tokens — needs a re-keying plan).
* Column-level encryption / restricted access to `features` snapshots; DB roles with least
  privilege (app role without DDL; migration role separate).
* Upload validation for dataset uploads (Data Management page, Phase 7) — size/type limits,
  schema validation before persistence, malware scanning.
* Alert routing (Alertmanager to an on-call system) and TLS for metrics scraping; the metrics
  token is a single shared secret without rotation. Grafana runs with a local admin account only.
* Dependency and container scanning in CI (pip-audit, Trivy), SBOM.
* Retention jobs, data subject request handling, formal threat model and penetration test.
