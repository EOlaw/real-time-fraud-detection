"use client";

import { useState } from "react";
import { type AnalyticsQuery, api, type Confusion } from "@/lib/api";
import { CASE_STATUS_LABEL, fmtInt, fmtPct, fmtUtc, LABEL_SOURCE_LABEL } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { Empty, ErrorState, Loading } from "./States";

function ConfusionTable({ title, c }: { title: string; c: Confusion }) {
  return (
    <div>
      <h4 className="subhead">{title}</h4>
      <table className="table kv compact">
        <tbody>
          <tr><th scope="row">Alerted &amp; labeled fraud (TP)</th><td>{fmtInt(c.true_positive)}</td></tr>
          <tr><th scope="row">Alerted &amp; labeled legitimate (FP)</th><td>{fmtInt(c.false_positive)}</td></tr>
          <tr><th scope="row">Allowed &amp; labeled fraud (FN)</th><td>{fmtInt(c.false_negative)}</td></tr>
          <tr><th scope="row">Allowed &amp; labeled legitimate (TN)</th><td>{fmtInt(c.true_negative)}</td></tr>
          <tr><th scope="row">Precision</th><td>{fmtPct(c.precision.rate)} ({c.precision.numerator} / {c.precision.denominator})</td></tr>
          <tr><th scope="row">Recall among labeled fraud</th><td>{fmtPct(c.recall.rate)} ({c.recall.numerator} / {c.recall.denominator})</td></tr>
          <tr><th scope="row">False-positive rate among labeled legitimate</th><td>{fmtPct(c.false_positive_rate.rate)} ({c.false_positive_rate.numerator} / {c.false_positive_rate.denominator})</td></tr>
        </tbody>
      </table>
    </div>
  );
}

/** Cases, labels and label-matured performance for the page's window and source filter. */
export function InvestigationAnalytics({ query, refreshKey }: { query: AnalyticsQuery | null; refreshKey: string }) {
  const [asOf, setAsOf] = useState(new Date().toISOString().slice(0, 16));
  const [maturity, setMaturity] = useState(30);
  const [probable, setProbable] = useState(false);
  const asOfIso = new Date(`${asOf}:00Z`).toISOString();
  const cases = useApi(query ? (t) => api.caseStats(t, query) : null, [refreshKey]);
  const labels = useApi(query ? (t) => api.labelStats(t, query) : null, [refreshKey]);
  const perf = useApi(
    query ? (t) => api.performance(t, query, asOfIso, maturity, probable) : null,
    [refreshKey, asOfIso, maturity, probable],
  );
  const c = cases.data;
  const p = perf.data;
  return (
    <>
      <h2 className="section-title">Investigations and labels</h2>
      <div className="grid-3">
        <section className="card" aria-labelledby="cases-h">
          <h3 id="cases-h" className="card-title">Cases (opened / resolved in window)</h3>
          {cases.error && <ErrorState error={cases.error} what="Case counts" />}
          {cases.loading && !c && <Loading />}
          {c && (
            <table className="table kv compact">
              <tbody>
                <tr><th scope="row">Opened</th><td>{fmtInt(c.opened)} ({Object.entries(c.opened_by_severity).map(([k, v]) => `${k} ${v}`).join(", ")})</td></tr>
                <tr><th scope="row">Resolved</th><td>{fmtInt(c.resolved)}</td></tr>
                {Object.entries(c.resolved_by_outcome).map(([k, v]) => (
                  <tr key={k}><th scope="row">· {CASE_STATUS_LABEL[k]}</th><td>{fmtInt(v)}</td></tr>
                ))}
                <tr><th scope="row">Time to resolution p50 / p90</th><td>{c.hours_to_resolution_p50 === null ? "n/a" : `${c.hours_to_resolution_p50.toFixed(1)} h / ${c.hours_to_resolution_p90!.toFixed(1)} h`}</td></tr>
                <tr><th scope="row">Open now (new / review / escalated)</th><td>{c.open_now_by_status.new} / {c.open_now_by_status.under_review} / {c.open_now_by_status.escalated} · {c.open_now_unassigned} unassigned</td></tr>
              </tbody>
            </table>
          )}
          <p className="footnote">Case outcomes are investigation results, not labels. Open-now counts are a snapshot at request time.</p>
        </section>
        <section className="card" aria-labelledby="labels-h">
          <h3 id="labels-h" className="card-title">Labels known in window (by labeled_at)</h3>
          {labels.error && <ErrorState error={labels.error} what="Label counts" />}
          {labels.loading && !labels.data && <Loading />}
          {labels.data &&
            (labels.data.recorded === 0 ? (
              <Empty>No labels became known in this window.</Empty>
            ) : (
              <div className="table-scroll" role="region" aria-label="Current labels table" tabIndex={0}>
              <table className="table compact">
                <thead>
                  <tr><th scope="col">Label</th><th scope="col">Source</th><th scope="col">Confidence</th><th scope="col">Current</th></tr>
                </thead>
                <tbody>
                  {labels.data.current.map((r) => (
                    <tr key={`${r.label}-${r.source}-${r.confidence}`}>
                      <td>{r.label}</td><td>{LABEL_SOURCE_LABEL[r.source]}</td><td>{r.confidence}</td><td>{fmtInt(r.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            ))}
          {labels.data && (
            <p className="footnote">
              {fmtInt(labels.data.recorded)} label rows recorded, {fmtInt(labels.data.corrections)} of them corrections. {labels.data.note}
            </p>
          )}
        </section>
        <section className="card" aria-labelledby="unl-h">
          <h3 id="unl-h" className="card-title">Unlabeled signals (no labels needed)</h3>
          {p && (
            <table className="table kv compact">
              <tbody>
                <tr><th scope="row">Assessments (event time in window)</th><td>{fmtInt(p.unlabeled_signals.assessments)}</td></tr>
                <tr><th scope="row">Alert rate (final)</th><td>{fmtPct(p.unlabeled_signals.alert_rate.rate)} ({p.unlabeled_signals.alert_rate.numerator} / {p.unlabeled_signals.alert_rate.denominator})</td></tr>
                <tr><th scope="row">Mean model probability</th><td>{p.unlabeled_signals.mean_risk_score?.toFixed(4) ?? "n/a"}</td></tr>
              </tbody>
            </table>
          )}
          <p className="footnote">{p?.unlabeled_signals.note ?? "Score and feature drift monitoring is Phase 9."}</p>
        </section>
      </div>

      <section className="card" aria-labelledby="perf-h">
        <h3 id="perf-h" className="card-title">Label-matured performance (labeled subset only)</h3>
        <form className="filters inline-form" onSubmit={(e) => e.preventDefault()} aria-label="Maturity settings">
          <label>
            Labels known as of (UTC)
            <input type="datetime-local" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
          </label>
          <label>
            Maturity (days)
            <input type="number" min={0} max={365} value={maturity} onChange={(e) => setMaturity(Number(e.target.value) || 0)} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={probable} onChange={(e) => setProbable(e.target.checked)} />
            Include probable labels
          </label>
        </form>
        {perf.error && <ErrorState error={perf.error} what="Performance" />}
        {perf.loading && !p && <Loading />}
        {p && (
          <>
            <p className="notice" role="note">{p.caveat}</p>
            <p className="kpi-sub">
              Cohort: transactions with event time in the window; events after {fmtUtc(p.maturity_cutoff)} are immature
              ({p.maturity_days} days before as-of) and excluded. Only labels known at {fmtUtc(p.as_of)} are used.
            </p>
            <table className="table kv compact">
              <tbody>
                <tr><th scope="row">Assessments in cohort</th><td>{fmtInt(p.population.assessments_in_window)}</td></tr>
                <tr><th scope="row">Excluded: immature</th><td>{fmtInt(p.population.immature)}</td></tr>
                <tr><th scope="row">Excluded: mature but unlabeled (not treated as legitimate)</th><td>{fmtInt(p.population.mature_unlabeled)}</td></tr>
                <tr><th scope="row">Excluded: inconclusive</th><td>{fmtInt(p.population.mature_inconclusive)}</td></tr>
                <tr><th scope="row">Excluded: probable (not requested)</th><td>{fmtInt(p.population.mature_probable_excluded)}</td></tr>
                <tr><th scope="row">Usable labeled (fraud / legitimate)</th><td>{fmtInt(p.population.usable)} ({p.population.usable_fraud} / {p.population.usable_legitimate})</td></tr>
                <tr><th scope="row">Label coverage of mature cohort</th><td>{fmtPct(p.population.label_coverage.rate)} ({p.population.label_coverage.numerator} / {p.population.label_coverage.denominator})</td></tr>
                <tr><th scope="row">Usable labels by source</th><td>{Object.entries(p.population.usable_by_source).map(([k, v]) => `${LABEL_SOURCE_LABEL[k] ?? k} ${v}`).join(", ") || "none"}</td></tr>
                <tr><th scope="row">Mean probability: labeled fraud / legitimate</th><td>{p.mean_score_fraud?.toFixed(4) ?? "n/a"} / {p.mean_score_legitimate?.toFixed(4) ?? "n/a"}</td></tr>
              </tbody>
            </table>
            {p.population.usable === 0 ? (
              <Empty>No usable mature labels for this cohort: no labeled performance can be reported.</Empty>
            ) : (
              <div className="grid-2">
                <ConfusionTable title="Final decision (after rules): alert = not allow" c={p.final_decision} />
                <ConfusionTable title="Policy decision (model-bound): alert = not allow" c={p.policy_decision} />
              </div>
            )}
          </>
        )}
      </section>
    </>
  );
}
