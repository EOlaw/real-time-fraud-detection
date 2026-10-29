import type { PredictionDetail, RuleEvaluation } from "@/lib/api";
import { DECISION_LABEL, escalated, fmtMs } from "@/lib/format";
import { SourceBadge } from "./Badges";

const OUTCOME: Record<RuleEvaluation["outcome"], string> = {
  matched: "Matched",
  not_matched: "Not matched",
  unknown: "Unknown (missing value)",
  error: "Error (fail-safe)",
};

/**
 * Model probability → model-bound policy decision → tenant rules → final decision, as stored at
 * scoring time. Rules act on the decision only; the probability shown is the model's, unchanged.
 */
export function DecisionTrace({ d }: { d: PredictionDetail }) {
  const up = escalated(d);
  const policyReasons = d.reasons.filter((r) => !r.startsWith("rule"));
  return (
    <div className="trace">
      <dl className="trace-steps">
        <div>
          <dt>Model fraud probability</dt>
          <dd>
            {d.risk_score === null ? (
              <span className="badge degraded">None: scoring degraded</span>
            ) : (
              <strong>{d.risk_score.toFixed(4)}</strong>
            )}
            <span className="muted">
              {" "}
              {d.model_name ? `${d.model_name}@${d.model_version}` : "no model"} · rules never change this value
            </span>
          </dd>
        </div>
        <div>
          <dt>Policy decision (model-bound)</dt>
          <dd>
            <span className={`badge ${d.policy_decision}`}>{DECISION_LABEL[d.policy_decision]}</span>
            <span className="muted">
              {" "}
              {d.policy_version}
              {policyReasons.length ? ` · ${policyReasons.join("; ")}` : ""}
            </span>
          </dd>
        </div>
        <div>
          <dt>Final decision (sandbox)</dt>
          <dd>
            <span className={`badge ${d.decision}`}>{DECISION_LABEL[d.decision]}</span>{" "}
            {d.rules.status === "none" && <span className="muted">No active rules when scored; final = policy decision.</span>}
            {d.rules.status !== "none" && !up && (
              <span className="muted">Rules evaluated; none made the decision stricter.</span>
            )}
            {up && d.rules.decided_by && (
              <span className="rule-flag">
                Escalated from {DECISION_LABEL[d.policy_decision]} by rule {d.rules.decided_by.rule_key}@v
                {d.rules.decided_by.version}
              </span>
            )}
            {d.rules.status === "error" && (
              <span className="rule-flag error">
                A rule failed to evaluate; the decision was escalated to at least review (fail-safe).
              </span>
            )}
          </dd>
        </div>
      </dl>
      {d.rule_evaluations.length > 0 && (
        <div className="table-scroll trace-table" role="region" aria-label="Rule evaluations table" tabIndex={0}>
          <table className="table">
            <caption className="sr-only">Rule versions evaluated for this assessment</caption>
            <thead>
              <tr>
                <th scope="col">Rule version</th>
                <th scope="col">Priority</th>
                <th scope="col">Action</th>
                <th scope="col">Outcome</th>
                <th scope="col">Decided</th>
              </tr>
            </thead>
            <tbody>
              {d.rule_evaluations.map((e) => (
                <tr key={e.rule_id}>
                  <td className="mono">
                    {e.rule_key}@v{e.version}
                  </td>
                  <td>{e.priority}</td>
                  <td>{e.action}</td>
                  <td className={`outcome ${e.outcome}`}>
                    {OUTCOME[e.outcome]}
                    {e.error_type ? ` · ${e.error_type}` : ""}
                  </td>
                  <td>{e.influenced_decision ? "Yes" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="footnote">
        <SourceBadge source={d.source} /> Trace stored with the assessment at {d.scored_at.slice(0, 19)}Z
        (scoring {fmtMs(d.processing_time_ms)}); later rule changes do not alter it.
      </p>
    </div>
  );
}
