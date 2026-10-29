import type { Decision, Source } from "@/lib/api";
import { DECISION_LABEL, escalated, SOURCE_HELP, SOURCE_LABEL } from "@/lib/format";

/** Source label, always visible: replayed and simulated traffic must never look live. */
export function SourceBadge({ source }: { source: Source }) {
  return (
    <span className={`src-badge ${source}`} title={SOURCE_HELP[source]}>
      {SOURCE_LABEL[source] ?? source}
    </span>
  );
}

/**
 * Final decision. When tenant rules escalated the model-bound policy decision, the policy decision
 * is shown next to it ("Review → Decline · rule") so the escalation is never hidden.
 */
export function DecisionBadge({
  decision,
  policyDecision,
  degraded,
}: {
  decision: Decision;
  policyDecision?: Decision | null;
  degraded?: boolean;
}) {
  const up = escalated({ decision, policy_decision: policyDecision });
  return (
    <span className="decision-cell">
      {up && policyDecision && (
        <>
          <span className="policy-was" title="Decision of the model-bound policy before tenant rules">
            {DECISION_LABEL[policyDecision]}
          </span>
          <span aria-hidden="true" className="arrow-right">→</span>
        </>
      )}
      <span className={`badge ${decision}`}>{DECISION_LABEL[decision]}</span>
      {degraded && <span className="badge degraded" title="Scoring failed; no model probability">Degraded</span>}
      {up && (
        <span className="rule-flag" title="A tenant rule made the final decision stricter than the policy decision">
          rule escalated
        </span>
      )}
    </span>
  );
}

export function SandboxNote({ compact }: { compact?: boolean }) {
  return (
    <p className={compact ? "sandbox-note compact" : "sandbox-note"}>
      <strong>Sandbox.</strong> Events come from a public synthetic card dataset (replayed or simulated) or
      clients labeled by source; decisions are simulated and never connected to money movement.
    </p>
  );
}
