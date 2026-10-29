"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AssessmentRow, type Decision, type Source } from "./api";
import { useAuth } from "./auth";
import { SseParser } from "./sse";

/** Allowlisted fields of a feed entry (GET /api/v1/stream/assessments). */
export interface FeedAssessment {
  assessment_id: string;
  transaction_id: string;
  status: "scored" | "degraded";
  risk_score: number | null;
  risk_level: string;
  decision: Decision;
  /** Present on entries published since Phase 6. */
  policy_decision?: Decision;
  model_version: string | null;
  policy_version: string;
  source: Source;
  scored_at: string;
  event_time: string;
  /** "stream" | "api" | "batch"; null for rows loaded from the database. */
  path: string | null;
  ingested_at: string | null;
  late_arrival: boolean | null;
}

export interface FeedItem extends FeedAssessment {
  /** Stream ID that delivered it (null when loaded from persisted data). */
  cursor: string | null;
  origin: "live" | "persisted";
  receivedAt: number;
}

export type FeedStatus =
  | "connecting"
  | "open"
  | "reconnecting"
  | "unavailable"
  | "forbidden"
  | "expired"
  | "paused";

export interface FeedStats {
  received: number;
  duplicates: number;
  connections: number;
  resets: number;
  lastEventAt: number | null;
  lastHeartbeatAt: number | null;
  cursor: string | null;
}

const MAX_ITEMS = 300;
/** Three missed server heartbeats (FRAUD_SSE_HEARTBEAT_SECONDS, default 15 s). */
const STALL_MS = 45_000;
const PERSISTED_PAGE = 50;

function fromRow(r: AssessmentRow): FeedItem {
  return {
    assessment_id: r.assessment_id,
    transaction_id: r.transaction_id,
    status: r.status,
    risk_score: r.risk_score,
    risk_level: r.risk_level,
    decision: r.decision,
    policy_decision: r.policy_decision,
    model_version: null,
    policy_version: "",
    source: r.source,
    scored_at: r.scored_at,
    event_time: r.event_time,
    path: null,
    ingested_at: null,
    late_arrival: null,
    cursor: null,
    origin: "persisted",
    receivedAt: Date.now(),
  };
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });

/**
 * Live assessments over an authenticated fetch-based SSE reader.
 *
 * * Connects first, then loads the latest persisted assessments, so nothing committed between the
 *   two is missed (overlaps are removed by assessment_id).
 * * Reconnects after a disconnect or server close with ``Last-Event-ID``; waits the server's
 *   ``retry`` (3 s), backing off to 30 s on repeated failures.
 * * Delivery is at least once: repeated assessment_ids are counted and dropped.
 * * ``reset`` (the cursor fell out of retained history) reloads persisted data.
 * * 401 ends the session with a visible notice; 403 and 503 are shown, not hidden.
 * * No bytes for 45 s (three missed heartbeats) is treated as a dead connection and reconnected.
 */
export function useAssessmentFeed() {
  const { session, logout } = useAuth();
  const token = session?.token ?? null;
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const [items, setItems] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [nextRetryAt, setNextRetryAt] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [stats, setStats] = useState<FeedStats>({
    received: 0,
    duplicates: 0,
    connections: 0,
    resets: 0,
    lastEventAt: null,
    lastHeartbeatAt: null,
    cursor: null,
  });
  const seen = useRef(new Set<string>());
  const cursor = useRef<string | null>(null);
  const attempt = useRef<AbortController | null>(null);

  const merge = useCallback((incoming: FeedItem[], countDuplicates: boolean) => {
    const fresh: FeedItem[] = [];
    let dups = 0;
    for (const it of incoming) {
      if (seen.current.has(it.assessment_id)) dups++;
      else {
        seen.current.add(it.assessment_id);
        fresh.push(it);
      }
    }
    if (countDuplicates && dups) setStats((s) => ({ ...s, duplicates: s.duplicates + dups }));
    if (fresh.length)
      setItems((prev) =>
        [...fresh, ...prev]
          .sort((a, b) => (a.scored_at < b.scored_at ? 1 : a.scored_at > b.scored_at ? -1 : 0))
          .slice(0, MAX_ITEMS),
      );
  }, []);

  const loadPersisted = useCallback(
    async (t: string) => {
      try {
        const page = await api.assessments(t, { sort: "scored_at", order: "desc", limit: PERSISTED_PAGE });
        merge(page.items.map(fromRow), false);
      } catch {
        setNotice("Could not load persisted assessments; showing live entries only.");
      }
    },
    [merge],
  );

  useEffect(() => {
    if (!token) return;
    if (paused) {
      setStatus("paused");
      return;
    }
    const life = new AbortController();
    let failures = 0;
    let retryMs = 3000;
    let first = true;
    let stalled = false;
    let watchdog: ReturnType<typeof setInterval> | undefined;

    const run = async () => {
      while (!life.signal.aborted) {
        const ctrl = new AbortController();
        attempt.current = ctrl;
        life.signal.addEventListener("abort", () => ctrl.abort());
        setNextRetryAt(null);
        setStatus(first ? "connecting" : "reconnecting");
        try {
          const headers: Record<string, string> = {
            Authorization: `Bearer ${token}`,
            Accept: "text/event-stream",
          };
          if (cursor.current) headers["Last-Event-ID"] = cursor.current;
          const res = await fetch("/api/v1/stream/assessments", {
            headers,
            signal: ctrl.signal,
            cache: "no-store",
          });
          if (res.status === 401) {
            setStatus("expired");
            logout("Your session expired while the live feed was open. Sign in again.");
            return;
          }
          if (res.status === 403) {
            setStatus("forbidden");
            setError("This account's role cannot read the live feed.");
            return;
          }
          if (!res.ok || !res.body) {
            const body = await res.json().catch(() => null);
            throw new Error(body?.error?.message ?? `HTTP ${res.status}`);
          }
          setStatus("open");
          setError(null);
          setStats((s) => ({ ...s, connections: s.connections + 1 }));
          failures = 0;
          if (first) {
            first = false;
            void loadPersisted(token);
          }
          const parser = new SseParser({
            onRetry: (ms) => (retryMs = ms),
            onComment: () => setStats((s) => ({ ...s, lastHeartbeatAt: Date.now() })),
            onMessage: (m) => {
              if (m.event === "reset") {
                setStats((s) => ({ ...s, resets: s.resets + 1 }));
                setNotice(
                  "The feed's retained history no longer covers this cursor; persisted assessments were reloaded.",
                );
                void loadPersisted(token);
                return;
              }
              if (m.event !== "assessment") return;
              let data: FeedAssessment;
              try {
                data = JSON.parse(m.data) as FeedAssessment;
              } catch {
                return;
              }
              cursor.current = m.id;
              setStats((s) => ({ ...s, received: s.received + 1, lastEventAt: Date.now(), cursor: m.id }));
              merge([{ ...data, cursor: m.id, origin: "live", receivedAt: Date.now() }], true);
            },
          });
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          // Watchdog: the server sends a heartbeat every 15 s. A proxy can keep the browser's
          // connection open after the API is gone, so silence means a dead connection.
          let lastBytes = Date.now();
          watchdog = setInterval(() => {
            if (Date.now() - lastBytes > STALL_MS) {
              stalled = true;
              ctrl.abort();
            }
          }, 5_000);
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            lastBytes = Date.now();
            parser.push(decoder.decode(value, { stream: true }));
          }
          // Server closed the connection (max duration): reconnect after `retry`.
        } catch (e) {
          if (life.signal.aborted) return;
          if (stalled || !ctrl.signal.aborted) {
            failures++;
            setError(
              stalled
                ? `No events or heartbeats for ${STALL_MS / 1000} s; the connection is presumed dead.`
                : e instanceof Error
                  ? e.message
                  : String(e),
            );
            setStatus("unavailable");
          }
        } finally {
          clearInterval(watchdog);
        }
        if (life.signal.aborted) return;
        // A manual reconnect (aborted attempt) skips the wait.
        const manual = ctrl.signal.aborted && !stalled;
        stalled = false;
        const wait = manual ? 0 : failures ? Math.min(30_000, retryMs * 2 ** (failures - 1)) : retryMs;
        if (wait > 0) {
          setNextRetryAt(Date.now() + wait);
          if (failures === 0) setStatus("reconnecting");
          await sleep(wait, life.signal);
        }
      }
    };
    void run();
    return () => life.abort();
  }, [token, paused, logout, merge, loadPersisted]);

  /** Drop the current connection and reconnect immediately with Last-Event-ID. */
  const reconnect = useCallback(() => attempt.current?.abort(), []);

  return { status, items, stats, error, notice, nextRetryAt, paused, setPaused, reconnect };
}
