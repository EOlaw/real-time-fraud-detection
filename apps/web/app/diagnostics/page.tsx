"use client";

import { useEffect, useState } from "react";
import { MonitoringPanel } from "@/components/MonitoringPanel";
import { ErrorState, Loading } from "@/components/States";
import { api, type Readiness } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { useApi } from "@/lib/hooks";

export default function DiagnosticsPage() {
  const { session } = useAuth();
  const role = session!.role;
  const [ready, setReady] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
  const model = useApi((t) => api.activeModel(t), []);
  const stream = useApi(CAN.streamStatus(role) ? (t) => api.streamStatus(t) : null, [checkedAt?.getTime()]);

  const check = () =>
    api
      .ready()
      .then((r) => {
        setReady(r);
        setError(null);
      })
      .catch(() => setError("API unreachable"))
      .finally(() => setCheckedAt(new Date()));

  useEffect(() => {
    check();
    const t = setInterval(check, 15_000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="page">
      <div className="page-header"><div><div className="eyebrow">SYSTEM / DIAGNOSTICS</div><h1>Service diagnostics</h1><p>Readiness checks for the scoring service and its dependencies.</p></div></div>
      <div className="section-heading"><div><h2>Service readiness</h2><p>Automatically refreshed every 15 seconds.</p></div></div>
      <div className="card">
        <div className="status-line" role="status" aria-live="polite">
          <span className={`dot ${error ? "bad" : ready?.status === "ready" ? "ok" : "bad"}`} aria-hidden="true" />
          {error ?? (ready ? (ready.status === "ready" ? "Ready" : "Not ready") : "Checking…")}
          {checkedAt && <span className="muted"> · checked {checkedAt.toLocaleTimeString()} (every 15 s)</span>}
        </div>
        {ready && (
          <table className="table kv">
            <tbody>
              {Object.entries(ready.checks).map(([k, v]) => (
                <tr key={k}>
                  <th scope="row">{k}</th>
                  <td className="mono">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="section-heading distribution-heading"><div><h2>Serving model</h2><p>From the model registry (read-only). Promotion is a separate, gated step.</p></div></div>
      <div className="card">
        {model.error && <ErrorState error={model.error} what="Serving model" />}
        {model.loading && !model.data && <Loading />}
        {model.data && (
          <table className="table kv">
            <tbody>
              <tr><th scope="row">Model</th><td className="mono">{model.data.name}@{model.data.version}</td></tr>
              <tr><th scope="row">Registry status</th><td>{model.data.status}</td></tr>
              <tr><th scope="row">Anomaly score</th><td>Offline-only baseline; not served, not used in decisions.</td></tr>
            </tbody>
          </table>
        )}
      </div>

      <div className="section-heading distribution-heading"><div><h2>Event stream</h2><p>Backlog, pending and dead letters across all shards (admin only; contains no event data).</p></div></div>
      <div className="card">
        {!CAN.streamStatus(role) && <p className="muted">Available to admins. Your role: {role}.</p>}
        {stream.error && <ErrorState error={stream.error} what="Stream status" />}
        {stream.data && (
          <table className="table kv">
            <tbody>
              <tr><th scope="row">Backlog (queued, not yet scored)</th><td>{stream.data.backlog_total}</td></tr>
              <tr><th scope="row">Pending (delivered, not yet acknowledged)</th><td>{stream.data.pending_total}</td></tr>
              <tr><th scope="row">Dead letters</th><td className={stream.data.dead_letters ? "error" : undefined}>{stream.data.dead_letters}</td></tr>
            </tbody>
          </table>
        )}
      </div>

      <div className="section-heading distribution-heading"><div><h2>Model monitoring</h2><p>Drift against the serving model&apos;s reference window and label-matured performance for your organization.</p></div></div>
      <MonitoringPanel />
    </div>
  );
}
