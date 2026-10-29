"use client";

import { useEffect, useState } from "react";
import { DecisionBadge, SandboxNote, SourceBadge } from "@/components/Badges";
import { Empty } from "@/components/States";
import { SOURCES, type Source } from "@/lib/api";
import { type FeedStatus, useAssessmentFeed } from "@/lib/feed";
import { fmtInt, SOURCE_LABEL } from "@/lib/format";

const STATUS: Record<FeedStatus, { label: string; tone: "ok" | "warn" | "bad" }> = {
  connecting: { label: "Connecting…", tone: "warn" },
  open: { label: "Live: connected", tone: "ok" },
  reconnecting: { label: "Disconnected: reconnecting", tone: "warn" },
  unavailable: { label: "Feed unavailable: retrying", tone: "bad" },
  forbidden: { label: "Not permitted", tone: "bad" },
  expired: { label: "Session expired", tone: "bad" },
  paused: { label: "Paused", tone: "warn" },
};

function ago(t: number | null, now: number): string {
  if (t === null) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)} min ago`;
}

export default function LivePage() {
  const feed = useAssessmentFeed();
  const [now, setNow] = useState(Date.now());
  const [shown, setShown] = useState<Source[]>([...SOURCES]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const st = STATUS[feed.status];
  const rows = feed.items.filter((i) => shown.includes(i.source));
  const retryIn = feed.nextRetryAt ? Math.max(0, Math.ceil((feed.nextRetryAt - now) / 1000)) : null;

  return (
    <div className="page wide">
      <div className="page-header">
        <div>
          <div className="eyebrow">RISK MONITORING / LIVE</div>
          <h1>Live assessments</h1>
          <p>
            Assessments as they are committed, from the tenant&apos;s feed (stream workers, single and batch scoring).
            Delivery is at least once; repeated assessments are dropped by id.
          </p>
        </div>
      </div>
      <SandboxNote />

      <section className="card live-status" aria-label="Feed connection">
        <div className="status-line" role="status" aria-live="polite">
          <span className={`dot ${st.tone}`} aria-hidden="true" />
          <strong>{st.label}</strong>
          {retryIn !== null && feed.status !== "open" && <span className="muted">next attempt in {retryIn}s</span>}
          {feed.error && <span className="error">{feed.error}</span>}
        </div>
        <dl className="feed-stats">
          <div>
            <dt>Events received</dt>
            <dd>{fmtInt(feed.stats.received)}</dd>
          </div>
          <div>
            <dt>Duplicates dropped</dt>
            <dd>{fmtInt(feed.stats.duplicates)}</dd>
          </div>
          <div>
            <dt>Connections</dt>
            <dd>{fmtInt(feed.stats.connections)}</dd>
          </div>
          <div>
            <dt>Resets</dt>
            <dd>{fmtInt(feed.stats.resets)}</dd>
          </div>
          <div>
            <dt>Last event</dt>
            <dd>{ago(feed.stats.lastEventAt, now)}</dd>
          </div>
          <div>
            <dt>Last heartbeat</dt>
            <dd>{ago(feed.stats.lastHeartbeatAt, now)}</dd>
          </div>
          <div>
            <dt>Cursor (Last-Event-ID)</dt>
            <dd className="mono">{feed.stats.cursor ?? "none yet"}</dd>
          </div>
        </dl>
        <div className="button-row">
          <button type="button" className="button ghost" onClick={() => feed.setPaused(!feed.paused)}>
            {feed.paused ? "Resume feed" : "Pause feed"}
          </button>
          <button
            type="button"
            className="button ghost"
            onClick={feed.reconnect}
            disabled={feed.paused || feed.status !== "open"}
            title="Drops the connection and resumes from the last received event"
          >
            Reconnect now
          </button>
        </div>
        {feed.notice && (
          <p className="notice" role="status">
            {feed.notice}
          </p>
        )}
      </section>

      <fieldset className="source-filter">
        <legend>Show sources</legend>
        {SOURCES.filter((s) => s !== "backfill").map((s) => (
          <label className="inline" key={s}>
            <input
              type="checkbox"
              checked={shown.includes(s)}
              onChange={() => setShown((c) => (c.includes(s) ? c.filter((x) => x !== s) : [...c, s]))}
            />
            <span className={`src-badge ${s}`}>{SOURCE_LABEL[s]}</span>
          </label>
        ))}
      </fieldset>

      <section className="card" aria-labelledby="feed-h">
        <h2 id="feed-h" className="card-title">
          Latest assessments ({fmtInt(rows.length)} shown, newest first)
        </h2>
        {rows.length === 0 ? (
          <Empty>
            {feed.status === "open"
              ? "Connected. No assessments yet: new ones appear here as soon as they are committed."
              : "No assessments to show."}
          </Empty>
        ) : (
          <div className="table-scroll live-table" role="region" aria-label="Latest assessments table" tabIndex={0}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Scored (UTC)</th>
                  <th scope="col">Transaction</th>
                  <th scope="col">Source</th>
                  <th scope="col">Path</th>
                  <th scope="col">Probability</th>
                  <th scope="col">Decision</th>
                  <th scope="col">Arrived via</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.assessment_id} className={r.origin === "live" ? "fresh" : undefined}>
                    <td>{r.scored_at.slice(11, 19)}</td>
                    <td className="mono">{r.transaction_id}</td>
                    <td>
                      <SourceBadge source={r.source} />
                    </td>
                    <td>
                      {r.path ?? "—"}
                      {r.late_arrival ? <span className="rule-flag"> late arrival</span> : null}
                    </td>
                    <td>{r.risk_score === null ? "n/a (degraded)" : r.risk_score.toFixed(3)}</td>
                    <td>
                      <DecisionBadge
                        decision={r.decision}
                        policyDecision={r.policy_decision}
                        degraded={r.status === "degraded"}
                      />
                    </td>
                    <td className="muted">{r.origin === "live" ? `feed ${r.cursor}` : "loaded from database"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
