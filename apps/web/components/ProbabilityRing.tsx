import type { Decision } from "@/lib/api";
import { DECISION_COLOR } from "@/lib/format";

export function ProbabilityRing({ score, decision }: { score: number | null; decision: Decision }) {
  const color = score === null ? "var(--muted)" : DECISION_COLOR[decision];
  const r = 15;
  const c = 2 * Math.PI * r;
  return (
    <svg width={38} height={38} viewBox="0 0 38 38" aria-label="fraud probability">
      <circle cx={19} cy={19} r={r} stroke="#3a3a3a" strokeWidth={1.5} fill="none" />
      {score !== null && (
        <circle
          cx={19}
          cy={19}
          r={r}
          stroke={color}
          strokeWidth={1.5}
          fill="none"
          strokeDasharray={`${c * score} ${c}`}
          transform="rotate(-90 19 19)"
        />
      )}
      <text x={19} y={19} textAnchor="middle" dominantBaseline="central" className="ring-text" fill={color}>
        {score === null ? "n/a" : `${Math.round(score * 100)}%`}
      </text>
    </svg>
  );
}
