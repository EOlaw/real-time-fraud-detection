import type { Decision, Source, Thresholds } from "./api";
import type { Zone } from "@/components/Gauge";

export const CATEGORY_CODES: Record<string, string> = {
  entertainment: "EN",
  food_dining: "FO",
  gas_transport: "GA",
  grocery_net: "GN",
  grocery_pos: "GP",
  health_fitness: "HE",
  home: "HO",
  kids_pets: "KI",
  misc_net: "MN",
  misc_pos: "MP",
  personal_care: "PE",
  shopping_net: "SN",
  shopping_pos: "SP",
  travel: "TR",
};

export const FEATURE_LABELS: Record<string, string> = {
  log_amount: "Payment Amount, $",
  hour_of_day: "Hour of Day",
  hour_sin: "Hour of Day (sin)",
  hour_cos: "Hour of Day (cos)",
  day_of_week: "Day of Week",
  is_night: "Night-time",
  cust_txn_count_1h: "# of TNX, Last 1 Hour",
  cust_txn_count_24h: "# of TNX, Last 24 Hours",
  cust_txn_count_7d: "# of TNX, Last 7 Days",
  cust_txn_count_30d: "# of TNX, Last 30 Days",
  cust_amt_sum_24h: "Amount Spent, Last 24 Hours",
  cust_amt_mean_30d: "Mean Amount, Last 30 Days",
  cust_amt_std_30d: "Std of Amount, Last 30 Days",
  amt_zscore_30d: "Amount z-score vs 30 Days",
  amt_to_mean_ratio_30d: "Amount / 30-Day Mean",
  seconds_since_prev_txn: "Seconds Since Previous TNX",
  cust_same_category_count_30d: "# of TNX in Category, Last 30 Days",
  category: "Merchant Category",
  channel: "Channel",
};

export const DECISION_LABEL: Record<Decision, string> = {
  allow: "Allow",
  challenge: "Challenge",
  review: "Review",
  decline: "Decline",
};

export const DECISION_COLOR: Record<Decision, string> = {
  allow: "#4db6ac",
  challenge: "#fdd835",
  review: "#ffa040",
  decline: "#ff6e4a",
};

/** Gauge zones are exactly the decision policy's bands. */
export function policyZones(t: Thresholds): Zone[] {
  return [
    { from: 0, to: t.challenge_at, label: "OK", color: DECISION_COLOR.allow },
    { from: t.challenge_at, to: t.review_at, label: "Challenge", color: DECISION_COLOR.challenge },
    { from: t.review_at, to: t.decline_at, label: "Suspicious", color: DECISION_COLOR.review },
    { from: t.decline_at, to: 1, label: "Fraud", color: DECISION_COLOR.decline },
  ];
}

export function fmtValue(v: number | string | null): string {
  if (v === null) return "n/a";
  if (typeof v === "string") return v;
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
}

export function fmtMoney(v: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(v);
}

export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });
}

export const SOURCE_LABEL: Record<Source, string> = {
  live: "Live",
  replay: "Replay",
  simulated: "Simulated",
  backfill: "Backfill",
};

export const SOURCE_HELP: Record<Source, string> = {
  live: "Labeled live by the submitting client (this sandbox has no real payment traffic)",
  replay: "Historical dataset events replayed through the API or stream",
  simulated: "Seeded synthetic events from the simulator",
  backfill: "Imported history; never scored, only used as feature history",
};

export const RISK_LEVELS = ["low", "medium", "high", "critical", "unknown"] as const;

export const RISK_COLOR: Record<string, string> = {
  low: DECISION_COLOR.allow,
  medium: DECISION_COLOR.challenge,
  high: DECISION_COLOR.review,
  critical: DECISION_COLOR.decline,
  unknown: "#8d969c",
};

export const SEVERITY: Record<Decision, number> = { allow: 0, challenge: 1, review: 2, decline: 3 };

/** True when tenant rules made the final decision stricter than the model-bound policy's. */
export function escalated(d: { decision: Decision; policy_decision?: Decision | null }): boolean {
  return !!d.policy_decision && SEVERITY[d.decision] > SEVERITY[d.policy_decision];
}

export function fmtPct(rate: number | null, digits = 1): string {
  return rate === null ? "n/a" : `${(rate * 100).toFixed(digits)}%`;
}

export function fmtMs(v: number | null): string {
  return v === null ? "n/a" : v < 10 ? `${v.toFixed(2)} ms` : `${v.toFixed(1)} ms`;
}

export function fmtInt(v: number): string {
  return v.toLocaleString("en-US");
}

/** ISO timestamp → "2026-10-27 08:00 UTC" (explicit UTC everywhere a window is shown). */
export function fmtUtc(iso: string): string {
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

export const CASE_STATUS_LABEL: Record<string, string> = {
  new: "New",
  under_review: "Under review",
  escalated: "Escalated",
  resolved_fraud: "Resolved: fraud",
  resolved_legitimate: "Resolved: legitimate",
  inconclusive: "Inconclusive",
};

export const LABEL_SOURCE_LABEL: Record<string, string> = {
  analyst_review: "Analyst review",
  chargeback: "Chargeback",
  customer_report: "Customer report",
  dataset: "Dataset (synthetic is_fraud)",
};

/** Index of the [lo, hi) bin containing v. */
export function binIndex(edges: number[], v: number): number | null {
  for (let i = 0; i < edges.length - 1; i++) if (v >= edges[i] && v < edges[i + 1]) return i;
  return null;
}
