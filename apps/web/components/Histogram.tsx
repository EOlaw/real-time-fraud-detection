"use client";

import { useWidth } from "@/lib/hooks";

interface Props {
  title: string;
  labels: string[];
  /** Full label per bar for tooltips (defaults to labels). */
  titles?: string[];
  shares: number[];
  /** Index of the bar containing the selected transaction's value. */
  marker: number | null;
}

const H = 60;
const PAD = { l: 25, r: 5, t: 4, b: 12 };

function niceStep(max: number): number {
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
}

export function Histogram({ title, labels, titles, shares, marker }: Props) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const max = Math.max(...shares, 0.001);
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const iw = Math.max(0, width - PAD.l - PAD.r);
  const ih = H - PAD.t - PAD.b;
  const bw = iw / Math.max(labels.length, 1);
  const y = (v: number) => PAD.t + ih - (v / top) * ih;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  // Thin axis labels so they never overlap (≈5.6px per character at 9.5px).
  const longest = Math.max(...labels.map((l) => l.length), 1);
  const every = Math.ceil((longest * 5.6 + 6) / Math.max(bw, 1));

  return (
    <div className="card hist">
      <div className="card-title" title={title}>
        {title}
      </div>
      <div ref={ref} className="hist-body">
        {width > 0 && (
          <svg width={width} height={H}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.l} x2={PAD.l + iw} y1={y(t)} y2={y(t)} className="gridline" />
                <text x={PAD.l - 4} y={y(t)} className="axis" textAnchor="end" dominantBaseline="middle">
                  {Number(t.toFixed(3))}
                </text>
              </g>
            ))}
            {shares.map((s, i) => (
              <rect
                key={labels[i]}
                x={PAD.l + i * bw + bw * 0.18}
                y={y(s)}
                width={bw * 0.64}
                height={Math.max(0, PAD.t + ih - y(s))}
                className="bar"
              >
                <title>{`${titles?.[i] ?? labels[i]}: ${(s * 100).toFixed(1)}%`}</title>
              </rect>
            ))}
            {marker !== null && (
              <line
                x1={PAD.l + (marker + 0.5) * bw}
                x2={PAD.l + (marker + 0.5) * bw}
                y1={PAD.t}
                y2={PAD.t + ih}
                className="marker"
              />
            )}
            <line x1={PAD.l} x2={PAD.l + iw} y1={PAD.t + ih} y2={PAD.t + ih} className="baseline" />
            {labels.map((l, i) =>
              i % every === 0 ? (
                <text key={l} x={PAD.l + (i + 0.5) * bw} y={H - 6} className="axis" textAnchor="middle">
                  {l}
                </text>
              ) : null,
            )}
          </svg>
        )}
      </div>
    </div>
  );
}
