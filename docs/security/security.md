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

## Not yet implemented (required before any real deployment)

* API rate limiting and login lockout (Phase 10, Redis-backed).
* TLS termination (ALB/ACM in AWS) and HSTS; currently plain HTTP on localhost.
* Asymmetric JWT (RS256/EdDSA via an identity provider) with key rotation; token revocation.
* Secrets manager (AWS Secrets Manager) instead of `.env`; KMS-managed tokenization key with rotation
  strategy (rotation changes tokens — needs a re-keying plan).
* Column-level encryption / restricted access to `features` snapshots; DB roles with least
  privilege (app role without DDL; migration role separate).
* Upload validation for dataset uploads (Data Management page, Phase 7) — size/type limits,
  schema validation before persistence, malware scanning.
* Dependency and container scanning in CI (pip-audit, Trivy), SBOM.
* Retention jobs, data subject request handling, formal threat model and penetration test.
