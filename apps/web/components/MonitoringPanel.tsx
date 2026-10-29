"use client";

import { useState } from "react";
import { api, type MonitoringMetric } from "@/lib/api";
import { fmtPct, fmtUtc } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { Empty, ErrorState, Loading } from "./States";

const STATUS_LABEL: Record<string, string> = {
  ok: "OK",
  warn: "Warn",
  alert: "Alert",
  insufficient_data: "Insufficient data",
  failed: "Failed",
};

function fmt(m: MonitoringMetric): string {
  if (m.value === null) return "n/a";
  if (m.metric.includes("rate") || m.metric.startsWith("precision") || m.metric.startsWith("recall") ||
      m.metric.startsWith("false_positive") || m.metric === "label_coverage")
    return fmtPct(m.value, 2);
  return m.value.toFixed(4);
}

function MetricTable({ rows, caption }: { rows: MonitoringMetric[]; caption: string }) {
  return (
    <div className="table-scroll" role="region" aria-label={caption} tabIndex={0}>
      <table className="table compact">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Segment</th>
            <th scope="col">Value</th>
            <th scope="col">95% interval</th>
            <th scope="col">n (current / reference)</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={`${m.metric}-${m.segment}`}>
              <td className="mono">{m.metric}</td>
              <td>{m.segment}</td>
              <td>{fmt(m)}</td>
              <td>{m.ci_low === null || m.ci_high === null ? "—" : `${m.ci_low.toFixed(4)}–${m.ci_high.toFixed(4)}`}</td>
              <td>
                {m.sample_size.toLocaleString()}
                {m.reference_size !== null ? ` / ${m.reference_size.toLocaleString()}` : ""}
              </td>
              <td>
                <span className={`mon ${m.status}`}>{STATUS_LABEL[m.status]}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Latest monitoring runs of the caller's organization (computed by the monitoring job). */
export function MonitoringPanel() {
  const runs = useApi((t) => api.monitoringRuns(t, 10), []);
  const [selected, setSelected] = useState<string | null>(null);
  const runId = selected ?? runs.data?.[0]?.id ?? null;
  const detail = useApi(runId ? (t) => api.monitoringRun(t, runId) : null, [runId]);
  const d = detail.data;
  const important = (m: MonitoringMetric) =>
    !m.labeled &&
    (m.status === "warn" || m.status === "alert" || !m.metric.startsWith("feature_") || m.segment !== "all");
  return (
    <div className="card">
      {runs.error && <ErrorState error={runs.error} what="Monitoring runs" />}
      {runs.loading && !runs.data && <Loading />}
      {runs.data && runs.data.length === 0 && (
        <Empty>
          No monitoring runs for this organization yet. Operators run them with{" "}
          <code>python -m fraud_api.monitoring.cli run</code>.
        </Empty>
      )}
      {runs.data && runs.data.length > 0 && (
        <label className="compact-label">
          Run
          <select value={runId ?? ""} onChange={(e) => setSelected(e.target.value)}>
            {runs.data.map((r) => (
              <option key={r.id} value={r.id}>
                {fmtUtc(r.window_start)} → {fmtUtc(r.window_end)} · {r.model} · {STATUS_LABEL[r.status]}
              </option>
            ))}
          </select>
        </label>
      )}
      {detail.error && <ErrorState error={detail.error} what="Monitoring run" />}
      {d && (
        <>
          <p className="kpi-sub">
            <span className={`mon ${d.status}`}>{STATUS_LABEL[d.status]}</span> · recommendation:{" "}
            <strong>{d.recommendation === "none" ? "none" : d.recommendation.replace("_", " ")}</strong>
            {d.acknowledged ? " (acknowledged)" : ""} · {d.summary.assessments ?? 0} assessments by {d.model}
            {d.summary.excluded_other_model_versions
              ? ` · ${d.summary.excluded_other_model_versions} scored by other model versions excluded`
              : ""}{" "}
            · reference: validation window {d.summary.reference?.window?.map((w) => w.slice(0, 10)).join(" to ")} (
            {d.summary.reference?.sample_size?.toLocaleString()} events) · {d.calc_version}
          </p>
          <p className="hint">
            Drift never promotes or retrains a model: an alert recommends a review. PSI: under 0.10 stable, 0.10–0.25 warn,
            0.25 or more alert. Fewer than 500 current values is reported as insufficient data, never OK.
          </p>
          <h3 className="subhead">Unlabeled distribution and operational signals</h3>
          <MetricTable caption="Unlabeled signals" rows={d.metrics.filter(important)} />
          <h3 className="subhead">Label-matured performance (labeled subset)</h3>
          <p className="hint">{d.summary.caveat}</p>
          <MetricTable caption="Label-matured performance" rows={d.metrics.filter((m) => m.labeled)} />
        </>
      )}
    </div>
  );
}
