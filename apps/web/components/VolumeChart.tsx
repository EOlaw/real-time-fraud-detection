"use client";

import { DECISIONS, type Decision, type VolumeBucket } from "@/lib/api";
import { DECISION_COLOR, DECISION_LABEL, fmtInt, fmtUtc } from "@/lib/format";
import { useWidth } from "@/lib/hooks";

const PAD = { l: 44, r: 8, t: 6, b: 4 };
const AXIS_H = 18;
const TX_COLOR = "#9aa6b0";

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const mag = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= v) ?? mag * 10;
}

interface Series {
  key: string;
  label: string;
  color: string;
  value: (b: VolumeBucket) => number;
}

/** One small multiple: stacked bars of ``series`` per bucket, on its own y scale. */
function Panel({
  buckets,
  series,
  width,
  height,
  title,
}: {
  buckets: VolumeBucket[];
  series: Series[];
  width: number;
  height: number;
  title: string;
}) {
  const top = niceMax(Math.max(...buckets.map((b) => series.reduce((s, x) => s + x.value(b), 0)), 1));
  const iw = Math.max(0, width - PAD.l - PAD.r);
  const ih = height - PAD.t - PAD.b;
  const bw = iw / Math.max(buckets.length, 1);
  const y = (v: number) => PAD.t + ih - (v / top) * ih;
  return (
    <svg width={width} height={height} role="img" aria-label={`${title}; exact values in the data table`}>
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={PAD.l} x2={PAD.l + iw} y1={y(top * f)} y2={y(top * f)} className="gridline" />
          <text x={PAD.l - 5} y={y(top * f)} className="axis" textAnchor="end" dominantBaseline="middle">
            {fmtInt(Math.round(top * f))}
          </text>
        </g>
      ))}
      {buckets.map((b, i) => {
        let acc = 0;
        return (
          <g key={b.start}>
            {series.map((s) => {
              const v = s.value(b);
              if (!v) return null;
              const y0 = y(acc);
              acc += v;
              return (
                <rect
                  key={s.key}
                  x={PAD.l + i * bw + bw * 0.12}
                  width={Math.max(1, bw * 0.76)}
                  y={y(acc)}
                  height={Math.max(1, y0 - y(acc))}
                  fill={s.color}
                >
                  <title>{`${fmtUtc(b.start)} · ${s.label}: ${fmtInt(v)}`}</title>
                </rect>
              );
            })}
          </g>
        );
      })}
      <line x1={PAD.l} x2={PAD.l + iw} y1={PAD.t + ih} y2={PAD.t + ih} className="baseline" />
    </svg>
  );
}

/**
 * Two small multiples sharing the time axis, each on its own scale: assessments stacked by final
 * decision, and transactions received (which include imported, never-scored history and can be
 * orders of magnitude larger).
 */
export function VolumeChart({ buckets, bucket }: { buckets: VolumeBucket[]; bucket: "hour" | "day" }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const iw = Math.max(0, width - PAD.l - PAD.r);
  const bw = iw / Math.max(buckets.length, 1);
  const every = Math.max(1, Math.ceil(buckets.length / Math.max(1, Math.floor(iw / 64))));
  const label = (iso: string) => (bucket === "hour" ? iso.slice(11, 16) : iso.slice(5, 10));
  const decisionSeries: Series[] = DECISIONS.map((d: Decision) => ({
    key: d,
    label: DECISION_LABEL[d],
    color: DECISION_COLOR[d],
    value: (b) => b.by_decision[d] ?? 0,
  }));
  const txSeries: Series[] = [
    { key: "tx", label: "Transactions received", color: TX_COLOR, value: (b) => b.transactions },
  ];

  return (
    <div>
      <div className="legend" aria-hidden="true">
        {DECISIONS.map((d) => (
          <span key={d}>
            <i style={{ background: DECISION_COLOR[d] }} /> {DECISION_LABEL[d]}
          </span>
        ))}
      </div>
      <div ref={ref} className="chart-body">
        {width > 0 && (
          <>
            <div className="panel-label">Assessments by final decision</div>
            <Panel buckets={buckets} series={decisionSeries} width={width} height={150} title="Assessments per bucket by final decision" />
            <div className="panel-label">
              <i style={{ background: TX_COLOR }} aria-hidden="true" /> Transactions received (includes imported history;
              own scale)
            </div>
            <Panel buckets={buckets} series={txSeries} width={width} height={70} title="Transactions received per bucket" />
            <svg width={width} height={AXIS_H} aria-hidden="true">
              {buckets.map((b, i) =>
                i % every === 0 ? (
                  <text key={b.start} x={PAD.l + (i + 0.5) * bw} y={12} className="axis" textAnchor="middle">
                    {label(b.start)}
                  </text>
                ) : null,
              )}
            </svg>
          </>
        )}
      </div>
      <details className="data-table">
        <summary>Data table</summary>
        <div className="table-scroll" role="region" aria-label="Volume data table" tabIndex={0}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Bucket start (UTC)</th>
                <th scope="col">Transactions</th>
                <th scope="col">Assessments</th>
                <th scope="col">Alerts</th>
                {DECISIONS.map((d) => (
                  <th scope="col" key={d}>
                    {DECISION_LABEL[d]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.start}>
                  <td>{fmtUtc(b.start)}</td>
                  <td>{fmtInt(b.transactions)}</td>
                  <td>{fmtInt(b.assessments)}</td>
                  <td>{fmtInt(b.alerts)}</td>
                  {DECISIONS.map((d) => (
                    <td key={d}>{fmtInt(b.by_decision[d] ?? 0)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
