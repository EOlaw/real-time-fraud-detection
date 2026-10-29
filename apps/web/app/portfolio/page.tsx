"use client";

import Link from "next/link";
import { DecisionBadge, SandboxNote, SourceBadge } from "@/components/Badges";
import { ProbabilityRing } from "@/components/ProbabilityRing";
import { Empty, ErrorState, Loading } from "@/components/States";
import { api } from "@/lib/api";
import { CATEGORY_CODES, fmtMoney, fmtTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";

export default function PortfolioPage() {
  const top = useApi((t) => api.assessments(t, { sort: "risk_score", order: "desc", limit: 15 }), []);
  const dist = useApi((t) => api.distributions(t), []);
  const rules = useApi((t) => api.rules(t), []);
  const t = dist.data?.thresholds;
  return (
    <div className="page">
      <div className="page-header">
        <div>
          <div className="eyebrow">RISK MONITORING / PORTFOLIO</div>
          <h1>Portfolio overview</h1>
          <p>
            A quick view of scored activity and the current decision policy. Time-windowed rates are on{" "}
            <Link href="/analytics" className="link">
              Analytics
            </Link>
            .
          </p>
        </div>
      </div>
      <SandboxNote />
      <div className="kpis">
        <div className="card kpi">
          <div className="card-title">Scored transactions (all time)</div>
          <div className="kpi-value">{top.data?.total.toLocaleString() ?? "—"}</div>
        </div>
        <div className="card kpi">
          <div className="card-title">Policy (model-bound)</div>
          <div className="kpi-value small">{dist.data?.policy_version ?? "—"}</div>
          <div className="kpi-sub">
            {t ? `Thresholds: challenge ${t.challenge_at} · review ${t.review_at} · decline ${t.decline_at}` : ""}
          </div>
        </div>
        <div className="card kpi">
          <div className="card-title">Active tenant rules</div>
          <div className="kpi-value">{rules.data ? rules.data.active_count : rules.error ? "n/a" : "—"}</div>
          <div className="kpi-sub">Rules can only escalate the policy decision.</div>
        </div>
      </div>
      <div className="section-heading">
        <div>
          <h2>Highest risk transactions</h2>
          <p>The 15 highest model probabilities among scored transactions.</p>
        </div>
      </div>
      <div className="card portfolio-table-card">
        {top.error && <ErrorState error={top.error} what="Transactions" />}
        {top.loading && !top.data && <Loading />}
        {top.data && top.data.items.length === 0 && <Empty>No scored transactions yet.</Empty>}
        {top.data && top.data.items.length > 0 && (
          <div className="table-scroll portfolio-table-scroll" role="region" aria-label="Highest risk transactions table" tabIndex={0}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Transaction Id</th>
                  <th scope="col">Fraud Probability</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Source</th>
                  <th scope="col">Customer Id</th>
                  <th scope="col">Amount</th>
                  <th scope="col">Category</th>
                  <th scope="col">Event time</th>
                </tr>
              </thead>
              <tbody>
                {top.data.items.map((r) => (
                  <tr key={r.assessment_id}>
                    <td className="link-text">{r.transaction_id}</td>
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
                    <td>{fmtTime(r.event_time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
