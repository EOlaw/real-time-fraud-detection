import type { ApiError } from "@/lib/api";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="state loading" role="status" aria-live="polite">
      {label}
    </p>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="state empty-state">{children}</p>;
}

export function ErrorState({ error, what }: { error: ApiError | string; what: string }) {
  const message = typeof error === "string" ? error : error.message;
  const code = typeof error === "string" ? null : error.code;
  return (
    <p className="state error-state" role="alert">
      {what} unavailable: {message}
      {code && code !== "http_error" && <span className="mono"> ({code})</span>}
    </p>
  );
}
