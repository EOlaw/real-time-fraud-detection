import { fmtInt, fmtPct } from "@/lib/format";

export interface BarRow {
  key: string;
  label: string;
  value: number;
  color: string;
  title?: string;
}

/**
 * Horizontal bars as a real table: label, bar (decorative), count and share of ``denominator``.
 * Screen readers get the numbers; the bar only repeats them visually.
 */
export function BarList({
  rows,
  denominator,
  caption,
}: {
  rows: BarRow[];
  denominator: number;
  caption: string;
}) {
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <table className="barlist">
      <caption className="sr-only">{caption}</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Category</th>
          <th scope="col">Bar</th>
          <th scope="col">Count</th>
          <th scope="col">Share</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key} title={r.title}>
            <th scope="row">{r.label}</th>
            <td className="barlist-bar" aria-hidden="true">
              <span style={{ width: `${(r.value / max) * 100}%`, background: r.color }} />
            </td>
            <td className="num">{fmtInt(r.value)}</td>
            <td className="num muted">{fmtPct(denominator ? r.value / denominator : null)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Policy decision vs final decision per category: makes rule escalations visible. */
export function DecisionComparison({
  policy,
  final,
  labels,
  colors,
  denominator,
}: {
  policy: Record<string, number>;
  final: Record<string, number>;
  labels: Record<string, string>;
  colors: Record<string, string>;
  denominator: number;
}) {
  const keys = Object.keys(labels);
  const max = Math.max(...keys.flatMap((k) => [policy[k] ?? 0, final[k] ?? 0]), 1);
  return (
    <table className="barlist comparison">
      <caption className="sr-only">Policy decision versus final decision counts</caption>
      <thead>
        <tr>
          <th scope="col">Decision</th>
          <th scope="col">Policy (model-bound)</th>
          <th scope="col">Final (after rules)</th>
          <th scope="col" className="num">Δ</th>
        </tr>
      </thead>
      <tbody>
        {keys.map((k) => {
          const p = policy[k] ?? 0;
          const f = final[k] ?? 0;
          return (
            <tr key={k}>
              <th scope="row">{labels[k]}</th>
              <td>
                <span className="mini-bar" aria-hidden="true">
                  <span style={{ width: `${(p / max) * 100}%`, background: colors[k], opacity: 0.55 }} />
                </span>
                {fmtInt(p)} <span className="muted">({fmtPct(denominator ? p / denominator : null)})</span>
              </td>
              <td>
                <span className="mini-bar" aria-hidden="true">
                  <span style={{ width: `${(f / max) * 100}%`, background: colors[k] }} />
                </span>
                {fmtInt(f)} <span className="muted">({fmtPct(denominator ? f / denominator : null)})</span>
              </td>
              <td className={`num ${f - p > 0 ? "delta-up" : f - p < 0 ? "delta-down" : "muted"}`}>
                {f - p > 0 ? `+${fmtInt(f - p)}` : fmtInt(f - p)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
