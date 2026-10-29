"use client";

import { type AssessmentRow, SOURCES, type Source } from "@/lib/api";
import { CATEGORY_CODES, fmtMoney, SOURCE_LABEL } from "@/lib/format";
import { DecisionBadge, SourceBadge } from "./Badges";
import { ProbabilityRing } from "./ProbabilityRing";

export type SortKey = "scored_at" | "risk_score" | "amount" | "event_time";

interface Props {
  rows: AssessmentRow[];
  total: number;
  selected: string | null;
  onSelect: (id: string) => void;
  sort: SortKey;
  order: "asc" | "desc";
  onSort: (key: SortKey) => void;
  search: string;
  onSearch: (q: string) => void;
  source: Source | "all";
  onSource: (s: Source | "all") => void;
  onMore: (() => void) | null;
  loading: boolean;
  error?: string | null;
}

function SortHeader(props: { label: string; k: SortKey; sort: SortKey; order: "asc" | "desc"; onSort: (k: SortKey) => void }) {
  const active = props.sort === props.k;
  return (
    <th scope="col" aria-sort={active ? (props.order === "asc" ? "ascending" : "descending") : undefined}>
      <button type="button" className={active ? "th-sort active" : "th-sort"} onClick={() => props.onSort(props.k)}>
        {props.label}
        <span className="arrow" aria-hidden="true">
          {active && props.order === "asc" ? "↑" : "↓"}
        </span>
      </button>
    </th>
  );
}

export function TransactionTable(p: Props) {
  return (
    <div className="card table-card">
      <div className="table-tools">
        <div className="search">
          <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
            <circle cx={11} cy={11} r={7} />
            <path d="m20 20-4-4" />
          </svg>
          <input
            placeholder="Search"
            value={p.search}
            onChange={(e) => p.onSearch(e.target.value)}
            aria-label="Search by transaction, customer or merchant id"
          />
        </div>
        <label className="compact-select">
          <span className="sr-only">Filter by source</span>
          <select value={p.source} onChange={(e) => p.onSource(e.target.value as Source | "all")}>
            <option value="all">All sources</option>
            {SOURCES.filter((s) => s !== "backfill").map((s) => (
              <option key={s} value={s}>
                {SOURCE_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="table-scroll">
        <table className="table">
          <caption className="sr-only">Scored transactions; select a row to see its decision trace and explanation</caption>
          <thead>
            <tr>
              <SortHeader label="Transaction Id" k="scored_at" sort={p.sort} order={p.order} onSort={p.onSort} />
              <SortHeader label="Fraud Probability" k="risk_score" sort={p.sort} order={p.order} onSort={p.onSort} />
              <th scope="col">Decision</th>
              <th scope="col">Source</th>
              <th scope="col">Customer Id</th>
              <SortHeader label="Amount" k="amount" sort={p.sort} order={p.order} onSort={p.onSort} />
              <th scope="col">Category</th>
            </tr>
          </thead>
          <tbody>
            {p.rows.map((r) => (
              <tr
                key={r.assessment_id}
                className={r.assessment_id === p.selected ? "selected" : undefined}
                aria-selected={r.assessment_id === p.selected}
                onClick={() => p.onSelect(r.assessment_id)}
              >
                <td>
                  <button type="button" className="link" onClick={() => p.onSelect(r.assessment_id)}>
                    {r.transaction_id}
                  </button>
                </td>
                <td>
                  <ProbabilityRing score={r.risk_score} decision={r.policy_decision} />
                </td>
                <td>
                  <DecisionBadge decision={r.decision} policyDecision={r.policy_decision} degraded={r.status === "degraded"} />
                </td>
                <td>
                  <SourceBadge source={r.source} />
                </td>
                <td className="mono">{r.customer_id}</td>
                <td>{fmtMoney(r.amount, r.currency)}</td>
                <td title={r.category}>{CATEGORY_CODES[r.category] ?? r.category}</td>
              </tr>
            ))}
            {p.loading && p.rows.length === 0 && (
              <tr>
                <td colSpan={7} className="muted empty" role="status">
                  Loading transactions…
                </td>
              </tr>
            )}
            {!p.loading && !p.error && p.rows.length === 0 && (
              <tr>
                <td colSpan={7} className="muted empty">
                  {p.search || p.source !== "all" ? "No transactions match these filters." : "No scored transactions yet."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="table-foot">
        <span className="muted" aria-live="polite">
          {p.rows.length} of {p.total.toLocaleString()} scored transactions
        </span>
        {p.onMore && (
          <button type="button" className="button ghost" onClick={p.onMore} disabled={p.loading}>
            Load more
          </button>
        )}
      </div>
    </div>
  );
}
