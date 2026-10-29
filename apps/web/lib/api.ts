// Typed client for the FastAPI backend. Requests go to this origin and are proxied (next.config.ts).

export type Decision = "allow" | "challenge" | "review" | "decline";
export type Source = "live" | "replay" | "simulated" | "backfill";
export type RulesStatus = "none" | "evaluated" | "error";
export type RuleAction = "flag" | "challenge" | "review" | "decline";

export const SOURCES: Source[] = ["live", "replay", "simulated", "backfill"];
export const DECISIONS: Decision[] = ["allow", "challenge", "review", "decline"];

export interface AssessmentRow {
  assessment_id: string;
  transaction_id: string;
  customer_id: string;
  merchant_id: string | null;
  amount: number;
  currency: string;
  category: string;
  channel: string;
  event_time: string;
  source: Source;
  status: "scored" | "degraded";
  risk_score: number | null;
  risk_level: string;
  /** Final action: the policy decision, escalated by tenant rules. */
  decision: Decision;
  /** Decision of the model-bound policy alone. */
  policy_decision: Decision;
  rules_status: RulesStatus;
  scored_at: string;
}

export interface AssessmentPage {
  total: number;
  limit: number;
  offset: number;
  items: AssessmentRow[];
}

export interface RuleRef {
  rule_id: string;
  rule_key: string;
  version: number;
  action: RuleAction;
}

export interface RulesSummary {
  status: RulesStatus;
  evaluated: number;
  matched: RuleRef[];
  errors: RuleRef[];
  decided_by: RuleRef | null;
}

export interface RuleEvaluation extends RuleRef {
  priority: number;
  outcome: "matched" | "not_matched" | "unknown" | "error";
  influenced_decision: boolean;
  error_type: string | null;
}

export interface PredictionDetail {
  assessment_id: string;
  transaction_id: string;
  status: "scored" | "degraded";
  risk_score: number | null;
  risk_level: string;
  decision: Decision;
  policy_decision: Decision;
  rules: RulesSummary;
  decision_mode: string;
  reasons: string[];
  model_name: string | null;
  model_version: string | null;
  policy_version: string;
  source: Source;
  processing_time_ms: number;
  scored_at: string;
  features: Record<string, number | string | null> | null;
  timings_ms: Record<string, number>;
  rule_evaluations: RuleEvaluation[];
}

export interface Contribution {
  feature: string;
  value: number | string | null;
  contribution_log_odds: number;
  normalized: number;
}

export interface Explanation {
  assessment_id: string;
  units: string;
  base_value_log_odds: number;
  margin_log_odds: number;
  probability: number;
  stored_risk_score: number | null;
  other_features_log_odds: number;
  contributions: Contribution[];
}

export interface Distribution {
  feature: string;
  title: string;
  kind: "categorical" | "numeric";
  bins: string[];
  edges: number[] | null;
  shares: number[];
}

export interface Thresholds {
  challenge_at: number;
  review_at: number;
  decline_at: number;
}

export interface DistributionsResponse {
  sample_size: number;
  policy_version: string;
  thresholds: Thresholds;
  distributions: Distribution[];
}

export interface Readiness {
  status: "ready" | "not_ready";
  checks: Record<string, string>;
}

// ---------------------------------------------------------------- analytics (Phase 6)

export type Basis = "processed" | "event";
export type Bucket = "hour" | "day";

export interface AnalyticsQuery {
  start: string;
  end: string;
  basis: Basis;
  sources: Source[];
}

export interface WindowOut {
  start: string;
  end: string;
  basis: Basis;
  sources: Source[];
}

export interface Rate {
  numerator: number;
  denominator: number;
  rate: number | null;
}

export interface Overview {
  window: WindowOut;
  transactions: { received: number; scored: number; unscored: number };
  assessments: {
    total: number;
    by_status: Record<string, number>;
    by_decision: Record<Decision, number>;
    by_policy_decision: Record<Decision, number>;
    by_risk_level: Record<string, number>;
    by_source: Record<Source, number>;
    by_rules_status: Record<RulesStatus, number>;
  };
  alert_rate: Rate;
  policy_alert_rate: Rate;
  rule_escalation_rate: Rate;
  degraded_rate: Rate;
}

export interface VolumeBucket {
  start: string;
  transactions: number;
  assessments: number;
  alerts: number;
  by_decision: Record<Decision, number>;
}

export interface VolumeOut {
  window: WindowOut;
  bucket: Bucket;
  buckets: VolumeBucket[];
}

export interface RuleHitRow {
  rule_id: string;
  rule_key: string;
  version: number;
  action: RuleAction;
  evaluated: number;
  matched: number;
  unknown: number;
  errors: number;
  influenced: number;
  match_rate: number | null;
}

export interface RuleHits {
  window: WindowOut;
  assessments: number;
  assessments_with_rules: number;
  assessments_with_match: number;
  assessments_escalated: number;
  rules: RuleHitRow[];
}

export interface LatencyDistribution {
  count: number;
  mean: number | null;
  p50: number | null;
  p90: number | null;
  p95: number | null;
  p99: number | null;
  max: number | null;
}

export interface Latency {
  window: WindowOut;
  processing_ms: LatencyDistribution;
  components_ms: Record<string, LatencyDistribution>;
}

// ---------------------------------------------------------------- rules (Phase 6)

export interface RuleVersion {
  id: string;
  rule_key: string;
  version: number;
  name: string;
  description: string | null;
  priority: number;
  action: RuleAction;
  condition: unknown;
  condition_sha256: string;
  created_by: string;
  created_at: string;
  active: boolean;
}

export interface RuleSummary {
  rule_key: string;
  latest_version: number;
  versions: number;
  active_version: number | null;
  active: RuleVersion | null;
  activated_at: string | null;
}

export interface RuleList {
  active_count: number;
  max_active: number;
  items: RuleSummary[];
}

export interface RuleDetail {
  rule_key: string;
  active_version: number | null;
  activated_at: string | null;
  versions: RuleVersion[];
}

export interface Activation {
  rule_key: string;
  active_version: number | null;
  previous_version: number | null;
  changed: boolean;
  activated_at: string | null;
}

export interface RuleCreate {
  rule_key: string;
  name: string;
  description?: string | null;
  priority: number;
  action: RuleAction;
  condition: unknown;
}

export interface Grammar {
  combinators: string[];
  operators: Record<string, string[]>;
  fields: Record<string, { type: string; nullable: boolean; description: string; values?: string[] }>;
  limits: Record<string, number>;
  logic: string;
}

export interface ModelVersionDetail {
  id: string;
  name: string;
  version: string;
  status: string;
  serving: boolean;
  [key: string]: unknown;
}

export interface StreamStatus {
  backlog_total: number;
  pending_total: number;
  dead_letters: number;
  [key: string]: unknown;
}

// ---------------------------------------------------------------- cases and labels (Phase 8)

export type CaseStatus = "new" | "under_review" | "escalated" | "resolved_fraud" | "resolved_legitimate" | "inconclusive";
export type Severity = "low" | "medium" | "high" | "critical";
export type LabelValue = "fraud" | "legitimate" | "inconclusive";
export type LabelSource = "analyst_review" | "chargeback" | "customer_report" | "dataset";
export type Confidence = "confirmed" | "probable";

export const CASE_STATUSES: CaseStatus[] = ["new", "under_review", "escalated", "resolved_fraud", "resolved_legitimate", "inconclusive"];
export const SEVERITIES: Severity[] = ["low", "medium", "high", "critical"];

export interface UserRef {
  id: string;
  email: string;
}

export interface LabelOut {
  id: number;
  transaction_id: string;
  label: LabelValue;
  source: LabelSource;
  confidence: Confidence;
  case_id: string | null;
  labeled_by: UserRef | null;
  labeled_at: string;
  recorded_at: string;
  supersedes_id: number | null;
  superseded_by: number | null;
  current: boolean;
  reason: string | null;
  provenance: Record<string, unknown>;
}

export interface LabelHistory {
  transaction_id: string;
  current: LabelOut | null;
  history: LabelOut[];
}

export interface CaseSummary {
  id: string;
  transaction_id: string;
  assessment_id: string;
  source: Source;
  severity: Severity;
  status: CaseStatus;
  assigned_to: UserRef | null;
  opened_by: UserRef;
  opened_at: string;
  resolved_at: string | null;
  updated_at: string;
  version: number;
  assessment_status: "scored" | "degraded";
  risk_score: number | null;
  policy_decision: Decision;
  decision: Decision;
  current_label: LabelValue | null;
}

export interface CasePage {
  total: number;
  limit: number;
  offset: number;
  items: CaseSummary[];
}

export interface CaseEvent {
  id: number;
  kind: "created" | "assigned" | "status_changed" | "resolved" | "reopened";
  from_value: string | null;
  to_value: string | null;
  actor: UserRef;
  case_version: number;
  created_at: string;
}

export interface CaseNote {
  id: number;
  author: UserRef;
  body: string;
  evidence: string[];
  created_at: string;
}

export interface CaseDetail extends CaseSummary {
  transaction: {
    transaction_id: string;
    customer_id: string;
    merchant_id: string | null;
    amount: number;
    currency: string;
    category: string;
    channel: string;
    event_time: string;
    source: Source;
  };
  model: { name: string | null; version: string | null; registry_status: string | null; serving: boolean };
  events: CaseEvent[];
  notes: CaseNote[];
  labels: LabelHistory;
  allowed_transitions: CaseStatus[];
}

export interface Assignee {
  id: string;
  email: string;
  role: "admin" | "analyst";
}

export interface LabelCreate {
  transaction_id: string;
  label: LabelValue;
  source: "analyst_review" | "chargeback" | "customer_report";
  confidence: Confidence;
  labeled_at: string;
  case_id?: string | null;
  supersedes_id?: number | null;
  reason?: string | null;
}

export interface CaseStats {
  window: WindowOut;
  opened: number;
  opened_by_severity: Record<Severity, number>;
  resolved: number;
  resolved_by_outcome: Record<string, number>;
  hours_to_resolution_p50: number | null;
  hours_to_resolution_p90: number | null;
  open_now_by_status: Record<string, number>;
  open_now_unassigned: number;
}

export interface LabelStats {
  window: WindowOut;
  recorded: number;
  corrections: number;
  current: { label: LabelValue; source: LabelSource; confidence: Confidence; count: number }[];
  note: string;
}

export interface Confusion {
  true_positive: number;
  false_positive: number;
  false_negative: number;
  true_negative: number;
  precision: Rate;
  recall: Rate;
  false_positive_rate: Rate;
}

export interface Performance {
  window: WindowOut;
  as_of: string;
  maturity_days: number;
  maturity_cutoff: string;
  include_probable: boolean;
  population: {
    assessments_in_window: number;
    immature: number;
    mature: number;
    mature_unlabeled: number;
    mature_inconclusive: number;
    mature_probable_excluded: number;
    usable: number;
    usable_fraud: number;
    usable_legitimate: number;
    label_coverage: Rate;
    usable_by_source: Record<string, number>;
  };
  final_decision: Confusion;
  policy_decision: Confusion;
  mean_score_fraud: number | null;
  mean_score_legitimate: number | null;
  unlabeled_signals: { assessments: number; alert_rate: Rate; mean_risk_score: number | null; note: string };
  caveat: string;
}

export interface CaseQuery {
  status?: CaseStatus[];
  severity?: Severity[];
  assigned?: "any" | "me" | "unassigned";
  source?: Source[];
  sort?: "opened_at" | "severity" | "updated_at";
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------- monitoring (Phase 9)

export interface MonitoringRun {
  id: string;
  model: string;
  window_start: string;
  window_end: string;
  labels_as_of: string | null;
  maturity_days: number | null;
  status: "ok" | "warn" | "alert" | "insufficient_data" | "failed";
  recommendation: "none" | "review" | "retraining_review";
  summary: {
    assessments?: number;
    degraded?: number;
    excluded_other_model_versions?: number;
    alerts?: string[];
    warnings?: string[];
    reference?: { window: string[]; sample_size: number; dataset_version: string };
    caveat?: string;
  };
  calc_version: string;
  finished_at: string;
  acknowledged: boolean;
}

export interface MonitoringMetric {
  metric: string;
  segment: string;
  value: number | null;
  ci_low: number | null;
  ci_high: number | null;
  sample_size: number;
  reference_size: number | null;
  labeled: boolean;
  status: MonitoringRun["status"];
  thresholds: Record<string, unknown>;
  details: Record<string, unknown>;
}

export interface MonitoringRunDetail extends MonitoringRun {
  metrics: MonitoringMetric[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: unknown = null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(path, { ...init, headers, cache: "no-store" });
  const body = await res.json().catch(() => null);
  if (!res.ok && !(path.startsWith("/health/ready") && body)) {
    const err = body?.error;
    throw new ApiError(
      res.status,
      err?.code ?? "http_error",
      err?.message ?? res.statusText,
      err?.details ?? null,
    );
  }
  return body as T;
}

function post<T>(path: string, token: string, body?: unknown): Promise<T> {
  return request<T>(path, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function windowParams(q: AnalyticsQuery, extra: Record<string, string> = {}): string {
  const qs = new URLSearchParams({ start: q.start, end: q.end, basis: q.basis, ...extra });
  for (const s of q.sources) qs.append("source", s);
  return qs.toString();
}

export const api = {
  login: (email: string, password: string) =>
    request<{ access_token: string; expires_in: number }>("/api/v1/auth/token", null, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  assessments: (
    token: string,
    params: {
      q?: string;
      sort?: string;
      order?: "asc" | "desc";
      limit?: number;
      offset?: number;
      sources?: Source[];
    },
  ) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (k === "sources" || v === undefined || v === "") continue;
      qs.set(k, String(v));
    }
    for (const s of params.sources ?? []) qs.append("source", s);
    return request<AssessmentPage>(`/api/v1/fraud/assessments?${qs}`, token);
  },
  prediction: (token: string, id: string) =>
    request<PredictionDetail>(`/api/v1/fraud/predictions/${id}`, token),
  explanation: (token: string, id: string, topK = 6) =>
    request<Explanation>(`/api/v1/fraud/predictions/${id}/explanation?top_k=${topK}`, token),
  distributions: (token: string, sample = 5000) =>
    request<DistributionsResponse>(`/api/v1/fraud/insights/distributions?sample=${sample}`, token),
  ready: () => request<Readiness>("/health/ready", null),

  overview: (token: string, q: AnalyticsQuery) =>
    request<Overview>(`/api/v1/fraud/insights/overview?${windowParams(q)}`, token),
  volume: (token: string, q: AnalyticsQuery, bucket: Bucket) =>
    request<VolumeOut>(`/api/v1/fraud/insights/volume?${windowParams(q, { bucket })}`, token),
  ruleHits: (token: string, q: AnalyticsQuery) =>
    request<RuleHits>(`/api/v1/fraud/insights/rule-hits?${windowParams(q)}`, token),
  latency: (token: string, q: AnalyticsQuery) =>
    request<Latency>(`/api/v1/fraud/insights/latency?${windowParams(q)}`, token),

  rules: (token: string) => request<RuleList>("/api/v1/rules", token),
  rule: (token: string, key: string) =>
    request<RuleDetail>(`/api/v1/rules/${encodeURIComponent(key)}`, token),
  grammar: (token: string) => request<Grammar>("/api/v1/rules/grammar", token),
  createRule: (token: string, body: RuleCreate) => post<RuleVersion>("/api/v1/rules", token, body),
  activateRule: (token: string, key: string, version: number, expected: number | "none") =>
    post<Activation>(
      `/api/v1/rules/${encodeURIComponent(key)}/versions/${version}/activate`,
      token,
      { expected_active_version: expected },
    ),
  deactivateRule: (token: string, key: string, expected: number) =>
    post<Activation>(`/api/v1/rules/${encodeURIComponent(key)}/deactivate`, token, {
      expected_active_version: expected,
    }),

  cases: (token: string, q: CaseQuery) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) {
      if (v === undefined) continue;
      if (Array.isArray(v)) v.forEach((x) => qs.append(k, String(x)));
      else qs.set(k, String(v));
    }
    return request<CasePage>(`/api/v1/cases?${qs}`, token);
  },
  caseDetail: (token: string, id: string) => request<CaseDetail>(`/api/v1/cases/${id}`, token),
  openCase: (token: string, assessmentId: string, note?: string) =>
    post<{ created: boolean; case: CaseDetail }>("/api/v1/cases", token, { assessment_id: assessmentId, note }),
  assignees: (token: string) => request<Assignee[]>("/api/v1/cases/assignees", token),
  assignCase: (token: string, id: string, assigneeId: string | null, version: number) =>
    post<CaseDetail>(`/api/v1/cases/${id}/assign`, token, { assignee_id: assigneeId, expected_version: version }),
  caseStatus: (token: string, id: string, status: "under_review" | "escalated", version: number) =>
    post<CaseDetail>(`/api/v1/cases/${id}/status`, token, { status, expected_version: version }),
  resolveCase: (
    token: string,
    id: string,
    body: { outcome: LabelValue; expected_version: number; note?: string; record_label: boolean; label_confidence: Confidence },
  ) => post<CaseDetail>(`/api/v1/cases/${id}/resolve`, token, body),
  addNote: (token: string, id: string, body: string, evidence: string[]) =>
    post<CaseNote>(`/api/v1/cases/${id}/notes`, token, { body, evidence }),
  labels: (token: string, transactionId: string) =>
    request<LabelHistory>(`/api/v1/labels?transaction_id=${encodeURIComponent(transactionId)}`, token),
  createLabel: (token: string, body: LabelCreate) => post<LabelOut>("/api/v1/labels", token, body),
  caseStats: (token: string, q: AnalyticsQuery) =>
    request<CaseStats>(`/api/v1/fraud/insights/cases?${windowParams(q)}`, token),
  labelStats: (token: string, q: AnalyticsQuery) =>
    request<LabelStats>(`/api/v1/fraud/insights/labels?${windowParams(q)}`, token),
  performance: (token: string, q: AnalyticsQuery, asOf: string, maturityDays: number, includeProbable: boolean) =>
    request<Performance>(
      `/api/v1/fraud/insights/performance?${windowParams(q, {
        as_of: asOf,
        maturity_days: String(maturityDays),
        include_probable: String(includeProbable),
      })}`,
      token,
    ),

  monitoringRuns: (token: string, limit = 10) =>
    request<MonitoringRun[]>(`/api/v1/monitoring/runs?limit=${limit}`, token),
  monitoringRun: (token: string, id: string) =>
    request<MonitoringRunDetail>(`/api/v1/monitoring/runs/${id}`, token),

  activeModel: (token: string) => request<ModelVersionDetail>("/api/v1/models/active", token),
  streamStatus: (token: string) => request<StreamStatus>("/api/v1/stream/status", token),
};
