import type { Contribution } from "@/lib/api";
import { FEATURE_LABELS, fmtValue } from "@/lib/format";

interface Props {
  contributions: Contribution[];
  /** Raw payment amount, shown instead of the model's log-amount input. */
  amount: number;
}

export function ShapBars({ contributions, amount }: Props) {
  return (
    <div className="shap">
      {contributions.map((c) => {
        const label = FEATURE_LABELS[c.feature] ?? c.feature;
        const value = c.feature === "log_amount" ? amount : c.value;
        const width = Math.max(Math.abs(c.normalized) * 100, 8);
        const toward = c.normalized >= 0 ? "toward fraud" : "toward legitimate";
        return (
          <div key={c.feature} className="shap-row">
            <div className="shap-label">
              {label}={fmtValue(value)}
            </div>
            <div
              className={c.normalized >= 0 ? "shap-bar" : "shap-bar neg"}
              style={{ width: `${width}%` }}
              title={`${c.contribution_log_odds.toFixed(3)} log-odds (${toward})`}
            >
              {c.normalized.toFixed(2).replace(/\.?0+$/, "") || "0"}
            </div>
          </div>
        );
      })}
    </div>
  );
}
