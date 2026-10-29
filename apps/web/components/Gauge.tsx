// Radial gauge: 240° sweep from lower-left (0) to lower-right (1), zones drawn from policy bands.

export interface Zone {
  from: number;
  to: number;
  label: string;
  color: string;
}

interface Props {
  value: number | null;
  /** Previous value, drawn as an outer arc so the change is visible on the scale. */
  previous?: number | null;
  zones: Zone[];
  disabled?: boolean;
  emptyText?: string;
}

const SWEEP = 240;
const START = -120;
const CX = 110;
const CY = 108;

function polar(v: number, r: number): [number, number] {
  const a = ((START + SWEEP * Math.min(1, Math.max(0, v))) * Math.PI) / 180;
  return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
}

function arc(from: number, to: number, r: number): string {
  const [x0, y0] = polar(from, r);
  const [x1, y1] = polar(to, r);
  const large = (to - from) * SWEEP > 180 ? 1 : 0;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

export function Gauge({ value, previous, zones, disabled, emptyText = "—" }: Props) {
  const ticks = Array.from({ length: 11 }, (_, i) => i / 10);
  const delta = value !== null && previous != null ? value - previous : null;
  const deltaText = delta === null ? null : Math.abs(delta) < 0.0005 ? "no change" : null;
  const band = 72;
  return (
    <svg viewBox="0 0 220 200" className="gauge" role="img" aria-label={`gauge ${value ?? "n/a"}`}>
      {ticks.map((t) => {
        const [x, y] = polar(t, 98);
        const [a, b] = polar(t, 88);
        const [c, d] = polar(t, 92);
        return (
          <g key={t}>
            <line x1={a} y1={b} x2={c} y2={d} stroke="var(--muted)" strokeWidth={1} />
            <text x={x} y={y} className="gauge-tick" textAnchor="middle" dominantBaseline="middle">
              {t === 0 || t === 1 ? t.toFixed(0) : t.toFixed(1)}
            </text>
          </g>
        );
      })}
      {disabled ? (
        <path d={arc(0, 1, band)} stroke="var(--panel-2)" strokeWidth={22} fill="none" />
      ) : (
        zones.map((z, i) => (
          <g key={z.label}>
            <path
              d={arc(z.from, z.to, band)}
              stroke={z.color}
              strokeWidth={22}
              fill="none"
              id={`zone-${i}`}
            />
            <path id={`zone-path-${z.label}`} d={arc(z.from, z.to, band - 4)} fill="none" />
            {(z.to - z.from) * SWEEP >= 26 && (
              <text className="gauge-zone">
                <textPath href={`#zone-path-${z.label}`} startOffset="50%" textAnchor="middle">
                  {z.label}
                </textPath>
              </text>
            )}
          </g>
        ))
      )}
      {!disabled && value !== null && (
        <path
          d={arc(Math.max(0, value - 0.035), Math.min(1, value + 0.035), band)}
          stroke="#5f5f5f"
          strokeWidth={24}
          fill="none"
        />
      )}
      {!disabled && value !== null && previous != null && previous !== value && (
        <path
          d={arc(Math.min(previous, value), Math.max(previous, value), 84)}
          stroke="var(--accent)"
          strokeWidth={4}
          fill="none"
        />
      )}
      <text x={CX} y={CY + 6} textAnchor="middle" className="gauge-value">
        {value === null ? emptyText : value.toFixed(3)}
      </text>
      {delta !== null && (
        <text
          x={CX}
          y={CY + 30}
          textAnchor="middle"
          className={deltaText ? "gauge-delta same" : delta >= 0 ? "gauge-delta up" : "gauge-delta down"}
        >
          {deltaText ?? `${delta >= 0 ? "▲" : "▼"}${Math.abs(delta).toFixed(3)}`}
        </text>
      )}
    </svg>
  );
}
