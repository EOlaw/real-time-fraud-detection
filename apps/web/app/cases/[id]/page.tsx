"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { SandboxNote, SourceBadge } from "@/components/Badges";
import { DecisionTrace } from "@/components/DecisionTrace";
import { ExplanationState } from "@/components/ExplanationState";
import { ShapBars } from "@/components/ShapBars";
import { Empty, ErrorState, Loading } from "@/components/States";
import {
  api,
  ApiError,
  type CaseDetail,
  type Confidence,
  type LabelCreate,
  type LabelValue,
} from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { CASE_STATUS_LABEL, fmtMoney, fmtTime, LABEL_SOURCE_LABEL } from "@/lib/format";
import { useApi } from "@/lib/hooks";

type Result = { text: string; kind: "ok" | "error" | "conflict" } | null;

function describe(e: unknown): Result {
  if (e instanceof ApiError) {
    if (e.code === "version_conflict" || e.code === "label_conflict")
      return { kind: "conflict", text: `${e.message} The latest data has been reloaded.` };
    return { kind: "error", text: `${e.message} (${e.code})` };
  }
  return { kind: "error", text: "Cannot reach the API" };
}

const EVENT_TEXT: Record<string, string> = {
  created: "opened the case",
  assigned: "changed the assignment",
  status_changed: "changed the status",
  resolved: "resolved the case",
  reopened: "reopened the case",
};

/** Turns user ids in assignment events into emails where we know them. */
function who(id: string | null, c: CaseDetail): string {
  if (!id) return "unassigned";
  const all = [c.opened_by, c.assigned_to, ...c.events.map((e) => e.actor), ...c.notes.map((n) => n.author)];
  return all.find((u) => u?.id === id)?.email ?? id.slice(0, 8);
}

function Actions({ c, onDone }: { c: CaseDetail; onDone: (r: Result) => void }) {
  const { session } = useAuth();
  const s = session!;
  const assignees = useApi(CAN.administerCases(s.role) ? (t) => api.assignees(t) : null, []);
  const [target, setTarget] = useState("");
  const [outcome, setOutcome] = useState<LabelValue>("fraud");
  const [note, setNote] = useState("");
  const [recordLabel, setRecordLabel] = useState(false);
  const [confidence, setConfidence] = useState<Confidence>("confirmed");
  const [busy, setBusy] = useState(false);
  const closed = c.resolved_at !== null;
  const canResolve = c.allowed_transitions.some((t) => ["resolved_fraud", "resolved_legitimate", "inconclusive"].includes(t));
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await fn();
      onDone({ kind: "ok", text: ok });
    } catch (e) {
      onDone(describe(e));
    } finally {
      setBusy(false);
    }
  };
  if (!CAN.workCases(s.role)) return <p className="muted">Read-only: viewers can inspect cases but not change them.</p>;
  return (
    <div className="case-actions">
      <div className="button-row">
        {!closed && !c.assigned_to && (
          <button type="button" className="button" disabled={busy}
            onClick={() => run(() => api.assignCase(s.token, c.id, s.userId, c.version), "Assigned to you.")}>
            Assign to me
          </button>
        )}
        {!closed && c.assigned_to?.id === s.userId && (
          <button type="button" className="button ghost" disabled={busy}
            onClick={() => run(() => api.assignCase(s.token, c.id, null, c.version), "Released.")}>
            Release case
          </button>
        )}
        {c.allowed_transitions
          .filter((t): t is "under_review" | "escalated" => t === "under_review" || t === "escalated")
          .map((t) => (
            <button key={t} type="button" className="button ghost" disabled={busy}
              onClick={() => run(() => api.caseStatus(s.token, c.id, t, c.version), `Status: ${CASE_STATUS_LABEL[t]}.`)}>
              {closed ? "Reopen (under review)" : `Move to ${CASE_STATUS_LABEL[t].toLowerCase()}`}
            </button>
          ))}
      </div>
      {CAN.administerCases(s.role) && !closed && (
        <div className="inline-form">
          <label>
            Assign to
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">Unassigned</option>
              {assignees.data?.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.email} ({u.role})
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="button ghost" disabled={busy}
            onClick={() => run(() => api.assignCase(s.token, c.id, target || null, c.version), "Assignment changed.")}>
            Set assignment
          </button>
        </div>
      )}
      {canResolve && (
        <form className="inline-form resolve" onSubmit={(e) => {
          e.preventDefault();
          void run(() => api.resolveCase(s.token, c.id, {
            outcome, expected_version: c.version, note: note.trim() || undefined, record_label: recordLabel, label_confidence: confidence,
          }), "Case resolved.");
        }}>
          <label>
            Outcome
            <select value={outcome} onChange={(e) => setOutcome(e.target.value as LabelValue)}>
              <option value="fraud">Fraud</option>
              <option value="legitimate">Legitimate</option>
              <option value="inconclusive">Inconclusive</option>
            </select>
          </label>
          <label className="grow">
            Resolution note (optional)
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={10000} />
          </label>
          <label className="inline">
            <input type="checkbox" checked={recordLabel} onChange={(e) => setRecordLabel(e.target.checked)} />
            Also record an analyst-review label
          </label>
          {recordLabel && (
            <label>
              Confidence
              <select value={confidence} onChange={(e) => setConfidence(e.target.value as Confidence)}>
                <option value="confirmed">Confirmed</option>
                <option value="probable">Probable</option>
              </select>
            </label>
          )}
          <button type="submit" className="button" disabled={busy}>
            Resolve case
          </button>
          <p className="hint">
            Resolving records the investigation outcome and never changes the stored decision. A label is a separate,
            provenance-tracked observation.
          </p>
        </form>
      )}
      {closed && !CAN.administerCases(s.role) && (
        <p className="muted">This case is closed. Only an admin can reopen it; notes and label corrections remain possible.</p>
      )}
      {c.allowed_transitions.length === 0 && !closed && c.assigned_to?.id !== s.userId && !CAN.administerCases(s.role) && (
        <p className="muted">Only the assigned analyst or an admin can change this case. You can still add notes.</p>
      )}
    </div>
  );
}

function LabelForm({ c, onDone }: { c: CaseDetail; onDone: (r: Result) => void }) {
  const { session } = useAuth();
  const cur = c.labels.current;
  const [value, setValue] = useState<LabelValue>("fraud");
  const [source, setSource] = useState<LabelCreate["source"]>("analyst_review");
  const [confidence, setConfidence] = useState<Confidence>("confirmed");
  const [labeledAt, setLabeledAt] = useState(new Date().toISOString().slice(0, 16));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.createLabel(session!.token, {
        transaction_id: c.transaction_id,
        label: value,
        source,
        confidence,
        labeled_at: new Date(`${labeledAt}:00Z`).toISOString(),
        case_id: source === "analyst_review" ? c.id : null,
        supersedes_id: cur?.id ?? null,
        reason: reason.trim() || null,
      });
      onDone({ kind: "ok", text: cur ? "Correction recorded; the earlier label is kept as history." : "Label recorded." });
      setReason("");
    } catch (err) {
      onDone(describe(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="inline-form" onSubmit={submit} aria-label={cur ? "Correct the current label" : "Record a label"}>
      <label>
        Label
        <select value={value} onChange={(e) => setValue(e.target.value as LabelValue)}>
          <option value="fraud">Fraud</option>
          <option value="legitimate">Legitimate</option>
          <option value="inconclusive">Inconclusive</option>
        </select>
      </label>
      <label>
        Source
        <select value={source} onChange={(e) => setSource(e.target.value as LabelCreate["source"])}>
          <option value="analyst_review">Analyst review (this case)</option>
          <option value="chargeback">Chargeback</option>
          <option value="customer_report">Customer report</option>
        </select>
      </label>
      <label>
        Confidence
        <select value={confidence} onChange={(e) => setConfidence(e.target.value as Confidence)}>
          <option value="confirmed">Confirmed</option>
          <option value="probable">Probable</option>
        </select>
      </label>
      <label>
        Known at (UTC)
        <input type="datetime-local" value={labeledAt} onChange={(e) => setLabeledAt(e.target.value)} required />
      </label>
      <label className="grow">
        Reason{cur ? " (required for a correction)" : " (optional)"}
        <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} required={!!cur} />
      </label>
      <button type="submit" className="button ghost" disabled={busy}>
        {cur ? `Correct label #${cur.id}` : "Record label"}
      </button>
    </form>
  );
}

export default function CaseDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { session } = useAuth();
  const role = session!.role;
  const detail = useApi((t) => api.caseDetail(t, id), [id]);
  const c = detail.data;
  const prediction = useApi(c ? (t) => api.prediction(t, c.assessment_id) : null, [c?.assessment_id, c?.version]);
  const explain = useApi(
    c && c.assessment_status === "scored" ? (t) => api.explanation(t, c.assessment_id) : null,
    [c?.assessment_id],
  );
  const [result, setResult] = useState<Result>(null);
  const [noteBody, setNoteBody] = useState("");
  const [busy, setBusy] = useState(false);
  const done = (r: Result) => {
    setResult(r);
    detail.reload();
  };

  if (detail.error)
    return (
      <div className="page wide">
        <ErrorState error={detail.error} what="Case" />
        <Link href="/cases" className="link">Back to the queue</Link>
      </div>
    );
  if (!c) return <div className="page wide"><Loading /></div>;

  return (
    <div className="page wide">
      <div className="page-header">
        <div>
          <div className="eyebrow">
            <Link href="/cases" className="link">INVESTIGATIONS / CASES</Link> / CASE
          </div>
          <h1>
            Case for <span className="mono">{c.transaction_id}</span>
          </h1>
          <p>
            <span className={`sev ${c.severity}`}>{c.severity}</span> · <strong>{CASE_STATUS_LABEL[c.status]}</strong> ·
            assigned to {c.assigned_to?.email ?? "nobody"} · opened {fmtTime(c.opened_at)} by {c.opened_by.email}
            {c.resolved_at && ` · resolved ${fmtTime(c.resolved_at)}`} · version {c.version}
          </p>
        </div>
      </div>
      <SandboxNote />
      {result && (
        <p className={result.kind === "ok" ? "notice ok" : result.kind === "conflict" ? "notice" : "error"} role={result.kind === "ok" ? "status" : "alert"}>
          {result.text}
        </p>
      )}

      <div className="split">
        <div>
          <section className="card" aria-labelledby="act-h">
            <h2 id="act-h" className="card-title">Investigation</h2>
            <Actions c={c} onDone={done} />
          </section>

          <section className="card" aria-labelledby="txn-h">
            <h2 id="txn-h" className="card-title">Transaction</h2>
            <table className="table kv">
              <tbody>
                <tr><th scope="row">Source</th><td><SourceBadge source={c.transaction.source} /></td></tr>
                <tr><th scope="row">Amount</th><td>{fmtMoney(c.transaction.amount, c.transaction.currency)}</td></tr>
                <tr><th scope="row">Category / channel</th><td>{c.transaction.category} / {c.transaction.channel}</td></tr>
                <tr><th scope="row">Customer (token)</th><td className="mono">{c.transaction.customer_id}</td></tr>
                <tr><th scope="row">Merchant</th><td>{c.transaction.merchant_id ?? "—"}</td></tr>
                <tr><th scope="row">Event time</th><td>{fmtTime(c.transaction.event_time)}</td></tr>
                <tr>
                  <th scope="row">Scoring model</th>
                  <td>
                    {c.model.version ? `${c.model.name}@${c.model.version} (${c.model.registry_status})` : "none (degraded)"}
                    {c.model.version && !c.model.serving && <span className="rule-flag"> not served by this API instance</span>}
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section className="card" aria-labelledby="trace-h">
            <h2 id="trace-h" className="card-title">Decision trace (stored at scoring time)</h2>
            {prediction.error && <ErrorState error={prediction.error} what="Assessment" />}
            {prediction.loading && !prediction.data && <Loading />}
            {prediction.data && <DecisionTrace d={prediction.data} />}
          </section>

          <section className="card" aria-labelledby="shap-h">
            <h2 id="shap-h" className="card-title">Important factors (normalized SHAP)</h2>
            {c.assessment_status === "degraded" && <p className="muted">Degraded assessment: there is no model score to explain.</p>}
            {explain.loading && !explain.data && <Loading />}
            {explain.error && <ExplanationState error={explain.error} />}
            {explain.data && (
              <>
                <ShapBars contributions={explain.data.contributions} amount={c.transaction.amount} />
                <p className="footnote">TreeSHAP log-odds contributions to the model probability. Model attribution, not causal evidence; rules and case outcomes are not part of it.</p>
              </>
            )}
          </section>
        </div>

        <div>
          <section className="card" aria-labelledby="labels-h">
            <h2 id="labels-h" className="card-title">Fraud labels (provenance)</h2>
            <p className="hint">
              A label is an observation with a source and a time it became known. Analyst dispositions describe a reviewed,
              selected population and are not automatic ground truth; dataset labels are the synthetic dataset&apos;s is_fraud.
              Corrections add a new label that supersedes the old one; nothing is overwritten.
            </p>
            {c.labels.history.length === 0 && <Empty>No label yet.</Empty>}
            {c.labels.history.length > 0 && (
              <ol className="label-chain">
                {[...c.labels.history].reverse().map((l) => (
                  <li key={l.id} className={l.current ? "current" : "superseded"}>
                    <div>
                      <strong>#{l.id} {l.label}</strong> · {l.confidence} · {LABEL_SOURCE_LABEL[l.source]}{" "}
                      {l.current ? <span className="badge allow">current</span> : <span className="muted">superseded by #{l.superseded_by}</span>}
                    </div>
                    <div className="muted">
                      known {fmtTime(l.labeled_at)} · recorded {fmtTime(l.recorded_at)} by {l.labeled_by?.email ?? "dataset import"}
                      {l.case_id ? ` · case ${l.case_id === c.id ? "(this case)" : l.case_id.slice(0, 8)}` : ""}
                    </div>
                    {l.reason && <div>Reason: {l.reason}</div>}
                  </li>
                ))}
              </ol>
            )}
            {CAN.workCases(role) && <LabelForm c={c} onDone={done} />}
          </section>

          <section className="card" aria-labelledby="notes-h">
            <h2 id="notes-h" className="card-title">Notes ({c.notes.length})</h2>
            {c.notes.length === 0 && <Empty>No notes yet.</Empty>}
            <ol className="notes">
              {c.notes.map((n) => (
                <li key={n.id}>
                  <div className="muted">{n.author.email} · {fmtTime(n.created_at)}</div>
                  <div className="note-body">{n.body}</div>
                  {n.evidence.length > 0 && <div className="muted">Evidence: {n.evidence.join(", ")}</div>}
                </li>
              ))}
            </ol>
            {CAN.workCases(role) && (
              <form className="inline-form" onSubmit={async (e) => {
                e.preventDefault();
                setBusy(true);
                try {
                  await api.addNote(session!.token, c.id, noteBody.trim(), []);
                  setNoteBody("");
                  done({ kind: "ok", text: "Note added." });
                } catch (err) {
                  done(describe(err));
                } finally {
                  setBusy(false);
                }
              }}>
                <label className="grow">
                  Add a note (notes cannot be edited or deleted)
                  <textarea value={noteBody} onChange={(e) => setNoteBody(e.target.value)} rows={3} maxLength={10000} required />
                </label>
                <button type="submit" className="button ghost" disabled={busy || !noteBody.trim()}>Add note</button>
              </form>
            )}
          </section>

          <section className="card" aria-labelledby="hist-h">
            <h2 id="hist-h" className="card-title">Status and assignment history</h2>
            <ol className="timeline">
              {c.events.map((e) => (
                <li key={e.id}>
                  <span className="muted">{fmtTime(e.created_at)} · v{e.case_version}</span>{" "}
                  <strong>{e.actor.email}</strong> {EVENT_TEXT[e.kind]}
                  {e.kind === "assigned"
                    ? `: ${who(e.from_value, c)} → ${who(e.to_value, c)}`
                    : e.from_value
                      ? `: ${CASE_STATUS_LABEL[e.from_value] ?? e.from_value} → ${CASE_STATUS_LABEL[e.to_value ?? ""] ?? e.to_value}`
                      : ""}
                </li>
              ))}
            </ol>
          </section>
        </div>
      </div>
    </div>
  );
}
