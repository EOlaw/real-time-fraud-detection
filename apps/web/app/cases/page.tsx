"use client";

import Link from "next/link";
import { useState } from "react";
import { DecisionBadge, SandboxNote, SourceBadge } from "@/components/Badges";
import { Empty, ErrorState, Loading } from "@/components/States";
import { api, CASE_STATUSES, type CaseQuery, type CaseStatus, SEVERITIES, type Severity } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { CASE_STATUS_LABEL, fmtTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";

const OPEN: CaseStatus[] = ["new", "under_review", "escalated"];
const PAGE = 50;

export default function CasesPage() {
  const { session } = useAuth();
  const role = session!.role;
  const [view, setView] = useState<"open" | "closed" | "all">("open");
  const [assigned, setAssigned] = useState<"any" | "me" | "unassigned">("any");
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [sort, setSort] = useState<"opened_at" | "severity" | "updated_at">("severity");
  const [offset, setOffset] = useState(0);
  const q: CaseQuery = {
    status: view === "open" ? OPEN : view === "closed" ? CASE_STATUSES.filter((s) => !OPEN.includes(s)) : undefined,
    assigned,
    severity: severity === "all" ? undefined : [severity],
    sort,
    order: "desc",
    limit: PAGE,
    offset,
  };
  const page = useApi((t) => api.cases(t, q), [JSON.stringify(q)]);

  return (
    <div className="page wide">
      <div className="page-header">
        <div>
          <div className="eyebrow">INVESTIGATIONS / CASES</div>
          <h1>Case queue</h1>
          <p>
            Investigations opened from scored transactions. A case status is the state of the investigation: it never
            changes the model probability, the policy decision or the final decision, and it is not a fraud label.
          </p>
        </div>
      </div>
      <SandboxNote />
      <p className="role-note">
        Signed in as <strong>{role}</strong>:{" "}
        {CAN.administerCases(role)
          ? "you can work, assign and reopen any case."
          : CAN.workCases(role)
            ? "you can open cases, claim unassigned ones, work your own, add notes and record labels."
            : "read-only."}{" "}
        <span className="muted">Open a case from a transaction on the Dashboard. The API enforces every permission.</span>
      </p>

      <form className="card filters" onSubmit={(e) => e.preventDefault()} aria-label="Queue filters">
        <label>
          Status
          <select value={view} onChange={(e) => { setView(e.target.value as typeof view); setOffset(0); }}>
            <option value="open">Open (new, under review, escalated)</option>
            <option value="closed">Closed</option>
            <option value="all">All</option>
          </select>
        </label>
        <label>
          Assignment
          <select value={assigned} onChange={(e) => { setAssigned(e.target.value as typeof assigned); setOffset(0); }}>
            <option value="any">Anyone</option>
            <option value="me">Assigned to me</option>
            <option value="unassigned">Unassigned</option>
          </select>
        </label>
        <label>
          Severity
          <select value={severity} onChange={(e) => { setSeverity(e.target.value as Severity | "all"); setOffset(0); }}>
            <option value="all">All</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Sort by
          <select value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
            <option value="severity">Severity</option>
            <option value="opened_at">Opened</option>
            <option value="updated_at">Last change</option>
          </select>
        </label>
        <button type="button" className="button ghost" onClick={page.reload}>
          Refresh
        </button>
      </form>

      <section className="card" aria-labelledby="queue-h">
        <h2 id="queue-h" className="card-title">
          {page.data ? `${page.data.total.toLocaleString()} cases` : "Cases"}
        </h2>
        {page.error && <ErrorState error={page.error} what="Case queue" />}
        {page.loading && !page.data && <Loading />}
        {page.data && page.data.items.length === 0 && (
          <Empty>No cases match these filters. Cases are opened from a transaction on the Dashboard.</Empty>
        )}
        {page.data && page.data.items.length > 0 && (
          <div className="table-scroll" role="region" aria-label="Case queue table" tabIndex={0}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Transaction</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Status</th>
                  <th scope="col">Assigned to</th>
                  <th scope="col">Probability</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Label</th>
                  <th scope="col">Source</th>
                  <th scope="col">Opened (UTC)</th>
                </tr>
              </thead>
              <tbody>
                {page.data.items.map((c) => (
                  <tr key={c.id}>
                    <td>
                      <Link className="link mono" href={`/cases/${c.id}`}>
                        {c.transaction_id}
                      </Link>
                    </td>
                    <td>
                      <span className={`sev ${c.severity}`}>{c.severity}</span>
                    </td>
                    <td>{CASE_STATUS_LABEL[c.status]}</td>
                    <td>{c.assigned_to?.email ?? <span className="muted">unassigned</span>}</td>
                    <td>{c.risk_score === null ? "n/a (degraded)" : c.risk_score.toFixed(3)}</td>
                    <td>
                      <DecisionBadge decision={c.decision} policyDecision={c.policy_decision} degraded={c.assessment_status === "degraded"} />
                    </td>
                    <td>{c.current_label ?? <span className="muted">none</span>}</td>
                    <td>
                      <SourceBadge source={c.source} />
                    </td>
                    <td>{fmtTime(c.opened_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {page.data && page.data.total > PAGE && (
          <div className="button-row">
            <button type="button" className="button ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Previous
            </button>
            <span className="muted">
              {offset + 1}–{Math.min(offset + PAGE, page.data.total)} of {page.data.total}
            </span>
            <button type="button" className="button ghost" disabled={offset + PAGE >= page.data.total} onClick={() => setOffset(offset + PAGE)}>
              Next
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
