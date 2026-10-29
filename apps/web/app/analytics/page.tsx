"use client";

import { useMemo, useState } from "react";
import { SandboxNote } from "@/components/Badges";
import { BarList, DecisionComparison } from "@/components/BarList";
import { InvestigationAnalytics } from "@/components/InvestigationAnalytics";
import { Empty, ErrorState, Loading } from "@/components/States";
import { VolumeChart } from "@/components/VolumeChart";
import {
  type AnalyticsQuery,
  api,
  type Basis,
  type Bucket,
  type LatencyDistribution,
  type Rate,
  SOURCES,
  type Source,
} from "@/lib/api";
import {
  DECISION_COLOR,
  DECISION_LABEL,
  fmtInt,
  fmtMs,
  fmtPct,
  fmtUtc,
  RISK_COLOR,
  RISK_LEVELS,
  SOURCE_HELP,
  SOURCE_LABEL,
} from "@/lib/format";
import { useApi } from "@/lib/hooks";

type Preset = "6h" | "24h" | "72h" | "7d" | "30d" | "custom";
const PRESETS: { value: Preset; label: string }[] = [
  { value: "6h", label: "Last 6 hours" },
  { value: "24h", label: "Last 24 hours" },
  { value: "72h", label: "Last 72 hours" },
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "custom", label: "Custom dates (UTC)" },
];
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const SOURCE_COLOR: Record<Source, string> = {
  live: "#7cc4ff",
  replay: "#b39ddb",
  simulated: "#80cbc4",
  backfill: "#8d969c",
};

/** Windows are aligned to UTC bucket boundaries, as the volume endpoint requires. */
function resolveWindow(preset: Preset, from: string, to: string, now: number) {
  if (preset === "custom") {
    const start = Date.parse(`${from}T00:00:00Z`);
    const end = Date.parse(`${to}T00:00:00Z`) + DAY; // the end date is inclusive in the form
    if (!from || !to || Number.isNaN(start) || Number.isNaN(end) || end <= start)
      return { error: "Choose a start date on or before the end date." } as const;
    if (end - start > 366 * DAY) return { error: "Windows are limited to 366 days." } as const;
    const bucket: Bucket = end - start <= 3 * DAY ? "hour" : "day";
    return { start, end, bucket } as const;
  }
  const hours = { "6h": 6, "24h": 24, "72h": 72 }[preset as "6h" | "24h" | "72h"];
  if (hours) {
    const end = Math.ceil(now / HOUR) * HOUR;
    return { start: end - hours * HOUR, end, bucket: "hour" as Bucket } as const;
  }
  const days = preset === "7d" ? 7 : 30;
  const end = Math.ceil(now / DAY) * DAY;
  return { start: end - days * DAY, end, bucket: "day" as Bucket } as const;
}

function RateCard({ title, rate, help }: { title: string; rate: Rate | undefined; help: string }) {
  return (
    <div className="card kpi">
      <div className="card-title">{title}</div>
      <div className="kpi-value">{rate ? fmtPct(rate.rate) : "—"}</div>
      <div className="kpi-sub">
        {rate ? `${fmtInt(rate.numerator)} / ${fmtInt(rate.denominator)} · ` : ""}
        {help}
      </div>
    </div>
  );
}

function LatencyRow({ label, d }: { label: string; d: LatencyDistribution }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{fmtInt(d.count)}</td>
      <td>{fmtMs(d.p50)}</td>
      <td>{fmtMs(d.p90)}</td>
      <td>{fmtMs(d.p95)}</td>
      <td>{fmtMs(d.p99)}</td>
      <td>{fmtMs(d.max)}</td>
      <td>{fmtMs(d.mean)}</td>
    </tr>
  );
}

export default function AnalyticsPage() {
  const [preset, setPreset] = useState<Preset>("24h");
  const today = new Date().toISOString().slice(0, 10);
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(today);
  const [basis, setBasis] = useState<Basis>("processed");
  const [sources, setSources] = useState<Source[]>([...SOURCES]);
  const [tick, setTick] = useState(0);

  const win = useMemo(
    () => resolveWindow(preset, from, to, Date.now()),
    // `tick` re-anchors relative windows to the current time on refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [preset, from, to, tick],
  );
  const query: AnalyticsQuery | null =
    "error" in win || sources.length === 0
      ? null
      : {
          start: new Date(win.start).toISOString(),
          end: new Date(win.end).toISOString(),
          basis,
          sources,
        };
  const key = query ? `${JSON.stringify(query)}#${tick}` : "none";
  const bucket: Bucket = "error" in win ? "hour" : win.bucket;

  const overview = useApi(query ? (t) => api.overview(t, query) : null, [key]);
  const volume = useApi(query ? (t) => api.volume(t, query, bucket) : null, [key, bucket]);
  const hits = useApi(query ? (t) => api.ruleHits(t, query) : null, [key]);
  const latency = useApi(query ? (t) => api.latency(t, query) : null, [key]);
  const o = overview.data;
  const total = o?.assessments.total ?? 0;

  function toggleSource(s: Source) {
    setSources((cur) =>
      cur.includes(s) ? cur.filter((x) => x !== s) : SOURCES.filter((x) => x === s || cur.includes(x)),
    );
  }

  return (
    <div className="page wide">
      <div className="page-header">
        <div>
          <div className="eyebrow">RISK MONITORING / ANALYTICS</div>
          <h1>Analytics</h1>
          <p>
            Aggregates computed by the API from stored assessments for the window below. Nothing here is precomputed,
            sampled or hardcoded.
          </p>
        </div>
      </div>
      <SandboxNote />

      <form className="card filters" onSubmit={(e) => e.preventDefault()} aria-label="Analytics filters">
        <label>
          Window
          <select value={preset} onChange={(e) => setPreset(e.target.value as Preset)}>
            {PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        {preset === "custom" && (
          <>
            <label>
              From (UTC date)
              <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label>
              To (UTC date, inclusive)
              <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
            </label>
          </>
        )}
        <fieldset>
          <legend>Time basis</legend>
          <label className="inline">
            <input type="radio" name="basis" checked={basis === "processed"} onChange={() => setBasis("processed")} />
            Processed (scored / received)
          </label>
          <label className="inline">
            <input type="radio" name="basis" checked={basis === "event"} onChange={() => setBasis("event")} />
            Event time
          </label>
        </fieldset>
        <fieldset>
          <legend>Sources</legend>
          {SOURCES.map((s) => (
            <label className="inline" key={s} title={SOURCE_HELP[s]}>
              <input type="checkbox" checked={sources.includes(s)} onChange={() => toggleSource(s)} />
              <span className={`src-badge ${s}`}>{SOURCE_LABEL[s]}</span>
            </label>
          ))}
        </fieldset>
        <button type="button" className="button ghost" onClick={() => setTick((t) => t + 1)}>
          Refresh
        </button>
      </form>

      <p className="window-line" aria-live="polite">
        {"error" in win ? (
          <span className="error">{win.error}</span>
        ) : sources.length === 0 ? (
          <span className="error">Select at least one source.</span>
        ) : (
          <>
            Window <strong>{fmtUtc(new Date(win.start).toISOString())}</strong> to{" "}
            <strong>{fmtUtc(new Date(win.end).toISOString())}</strong> (start inclusive, end exclusive) ·{" "}
            {basis === "processed" ? "by processing time" : "by event time"} · {bucket === "hour" ? "hourly" : "daily"}{" "}
            buckets · sources: {sources.map((s) => SOURCE_LABEL[s]).join(", ")}
          </>
        )}
      </p>

      {overview.error && <ErrorState error={overview.error} what="Overview" />}
      <div className="kpis six">
        <div className="card kpi">
          <div className="card-title">Transactions in window</div>
          <div className="kpi-value">{o ? fmtInt(o.transactions.received) : "—"}</div>
          <div className="kpi-sub">
            {o ? `${fmtInt(o.transactions.scored)} scored · ${fmtInt(o.transactions.unscored)} unscored (imported history)` : ""}
          </div>
        </div>
        <div className="card kpi">
          <div className="card-title">Assessments</div>
          <div className="kpi-value">{o ? fmtInt(total) : "—"}</div>
          <div className="kpi-sub">{o ? `${fmtInt(o.assessments.by_status.degraded ?? 0)} degraded (no score)` : ""}</div>
        </div>
        <RateCard title="Alert rate (final)" rate={o?.alert_rate} help="decision ≠ allow" />
        <RateCard title="Alert rate (policy only)" rate={o?.policy_alert_rate} help="policy decision ≠ allow" />
        <RateCard title="Escalated by rules" rate={o?.rule_escalation_rate} help="final ≠ policy decision" />
        <RateCard title="Degraded" rate={o?.degraded_rate} help="no model score" />
      </div>
      {overview.loading && !o && <Loading />}

      <section className="card chart-card" aria-labelledby="vol-h">
        <h2 id="vol-h" className="card-title">
          Volume and final decisions per {bucket} (UTC)
        </h2>
        {volume.error && <ErrorState error={volume.error} what="Volume" />}
        {volume.loading && !volume.data && <Loading />}
        {volume.data &&
          (volume.data.buckets.every((b) => b.assessments === 0 && b.transactions === 0) ? (
            <Empty>No transactions or assessments in this window for the selected sources.</Empty>
          ) : (
            <VolumeChart buckets={volume.data.buckets} bucket={volume.data.bucket} />
          ))}
      </section>

      <div className="grid-3">
        <section className="card" aria-labelledby="dec-h">
          <h2 id="dec-h" className="card-title">
            Policy decision vs final decision
          </h2>
          {o && total > 0 ? (
            <>
              <DecisionComparison
                policy={o.assessments.by_policy_decision}
                final={o.assessments.by_decision}
                labels={DECISION_LABEL}
                colors={DECISION_COLOR}
                denominator={total}
              />
              <p className="footnote">
                Shares are of {fmtInt(total)} assessments. Rules can only escalate, so changes move toward decline. The
                model probability is never changed by rules.
              </p>
            </>
          ) : o ? (
            <Empty>No assessments in this window.</Empty>
          ) : null}
        </section>
        <section className="card" aria-labelledby="risk-h">
          <h2 id="risk-h" className="card-title">
            Risk level (model-bound policy)
          </h2>
          {o && total > 0 ? (
            <BarList
              caption="Assessments by risk level"
              denominator={total}
              rows={RISK_LEVELS.map((l) => ({
                key: l,
                label: l === "unknown" ? "unknown (degraded)" : l,
                value: o.assessments.by_risk_level[l] ?? 0,
                color: RISK_COLOR[l],
              }))}
            />
          ) : o ? (
            <Empty>No assessments in this window.</Empty>
          ) : null}
        </section>
        <section className="card" aria-labelledby="src-h">
          <h2 id="src-h" className="card-title">
            Assessments by source
          </h2>
          {o && total > 0 ? (
            <BarList
              caption="Assessments by source label"
              denominator={total}
              rows={SOURCES.map((s) => ({
                key: s,
                label: SOURCE_LABEL[s],
                value: o.assessments.by_source[s] ?? 0,
                color: SOURCE_COLOR[s],
                title: SOURCE_HELP[s],
              }))}
            />
          ) : o ? (
            <Empty>No assessments in this window.</Empty>
          ) : null}
          <p className="footnote">Backfill rows are imported history and are never scored.</p>
        </section>
      </div>

      <section className="card" aria-labelledby="hits-h">
        <h2 id="hits-h" className="card-title">
          Rule hits
        </h2>
        {hits.error && <ErrorState error={hits.error} what="Rule hits" />}
        {hits.loading && !hits.data && <Loading />}
        {hits.data && (
          <>
            <p className="kpi-sub">
              {fmtInt(hits.data.assessments_with_rules)} of {fmtInt(hits.data.assessments)} assessments had active rules ·{" "}
              {fmtInt(hits.data.assessments_with_match)} matched at least one · {fmtInt(hits.data.assessments_escalated)}{" "}
              escalated by a rule
            </p>
            {hits.data.rules.length === 0 ? (
              <Empty>No rule evaluations in this window.</Empty>
            ) : (
              <div className="table-scroll" role="region" aria-label="Rule hits table" tabIndex={0}>
                <table className="table">
                  <thead>
                    <tr>
                      <th scope="col">Rule version</th>
                      <th scope="col">Action</th>
                      <th scope="col">Evaluated</th>
                      <th scope="col">Matched</th>
                      <th scope="col">Match rate</th>
                      <th scope="col">Unknown</th>
                      <th scope="col">Errors</th>
                      <th scope="col">Escalated decision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {hits.data.rules.map((r) => (
                      <tr key={r.rule_id}>
                        <td className="mono">
                          {r.rule_key}@v{r.version}
                        </td>
                        <td>{r.action}</td>
                        <td>{fmtInt(r.evaluated)}</td>
                        <td>{fmtInt(r.matched)}</td>
                        <td>{fmtPct(r.match_rate)}</td>
                        <td>{fmtInt(r.unknown)}</td>
                        <td className={r.errors ? "error" : undefined}>{fmtInt(r.errors)}</td>
                        <td>{fmtInt(r.influenced)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      <section className="card" aria-labelledby="lat-h">
        <h2 id="lat-h" className="card-title">
          Processing latency (server-side scoring)
        </h2>
        {latency.error && <ErrorState error={latency.error} what="Latency" />}
        {latency.loading && !latency.data && <Loading />}
        {latency.data &&
          (latency.data.processing_ms.count === 0 ? (
            <Empty>No assessments in this window.</Empty>
          ) : (
            <div className="table-scroll" role="region" aria-label="Latency table" tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Stage</th>
                    <th scope="col">Count</th>
                    <th scope="col">p50</th>
                    <th scope="col">p90</th>
                    <th scope="col">p95</th>
                    <th scope="col">p99</th>
                    <th scope="col">Max</th>
                    <th scope="col">Mean</th>
                  </tr>
                </thead>
                <tbody>
                  <LatencyRow label="Total (validation → decision)" d={latency.data.processing_ms} />
                  {Object.entries(latency.data.components_ms).map(([k, d]) => (
                    <LatencyRow key={k} label={k[0].toUpperCase() + k.slice(1)} d={d} />
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        <p className="footnote">
          Stored per assessment at scoring time. Excludes the database commit, network and stream queueing, so it is not
          end-to-end latency. Percentiles use linear interpolation.
        </p>
      </section>
      <InvestigationAnalytics query={query} refreshKey={key} />
    </div>
  );
}
