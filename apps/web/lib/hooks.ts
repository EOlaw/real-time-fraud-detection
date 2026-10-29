"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import { useAuth } from "./auth";

/** Width of an element, tracked with ResizeObserver (charts render at real pixel size). */
export function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export interface Loadable<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

/** Runs an authenticated request whenever ``deps`` change; a 401 ends the session. */
export function useApi<T>(
  fetcher: ((token: string) => Promise<T>) | null,
  deps: React.DependencyList,
): Loadable<T> {
  const { session, logout } = useAuth();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  const token = session?.token ?? null;

  useEffect(() => {
    if (!token || !fetcher) {
      setData(null);
      return;
    }
    let live = true;
    setLoading(true);
    fetcher(token)
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!live) return;
        const err = e instanceof ApiError ? e : new ApiError(0, "network", String(e));
        if (err.status === 401) logout("Your session is no longer valid. Sign in again.");
        setData(null);
        setError(err);
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}
