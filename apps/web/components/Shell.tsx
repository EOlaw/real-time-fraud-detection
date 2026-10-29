"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { ApiError } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";

const NAV = [
  { href: "/portfolio", label: "Portfolio", icon: "M3 3h18v18H3z M3 9h18 M9 9v12" },
  {
    href: "/dashboard",
    label: "Dashboard",
    icon: "M3 13h2v8H3zm4-6h2v14H7zm4 3h2v11h-2zm4-7h2v18h-2zm4 9h2v9h-2z",
  },
  { href: "/analytics", label: "Analytics", icon: "M3 3v18h18 M7 15l4-4 3 3 5-6" },
  { href: "/live", label: "Live", icon: "M12 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0 M6.3 6.3a8 8 0 0 0 0 11.4 M17.7 6.3a8 8 0 0 1 0 11.4" },
  { href: "/cases", label: "Cases", icon: "M4 7h16v12H4z M9 7V5h6v2 M4 12h16" },
  { href: "/rules", label: "Rules", icon: "M4 6h16 M4 12h10 M4 18h7 M17 15l2 2 3-4" },
  {
    href: "/diagnostics",
    label: "Diagnostics",
    icon: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0-6v3m0 14v3M4.2 4.2l2.1 2.1m11.4 11.4 2.1 2.1M2 12h3m14 0h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  },
  {
    href: "/settings",
    label: "Settings",
    icon: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2-1.2L14.5 3h-4l-.4 2.6a7.6 7.6 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7.6 7.6 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z",
  },
];

function Header() {
  const path = usePathname();
  const { session } = useAuth();
  return (
    <header className="header">
      <div className="header-inner">
        <Link href="/dashboard" className="brand" aria-label="Transaction Fraud Detection dashboard">
          <div className="logo" aria-hidden="true">FI<span>.</span></div>
          <div className="brand-copy">
            <div className="brand-title">Transaction Fraud Detection</div>
            <div className="brand-sub">Fraud detection in card payments · Sandbox</div>
          </div>
        </Link>
        <nav className="nav" aria-label="Main navigation">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} aria-current={path === n.href ? "page" : undefined} className={path === n.href || path.startsWith(`${n.href}/`) ? "nav-item active" : "nav-item"}>
              <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d={n.icon} />
              </svg>
              {n.label}
            </Link>
          ))}
        </nav>
        {session && (
          <div className="session-chip" title={session.email}>
            <span className="sr-only">Signed in as {session.email}, role </span>
            {session.role}
          </div>
        )}
      </div>
    </header>
  );
}

function Login() {
  const { login, notice } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Cannot reach the API");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card login" onSubmit={submit}>
      <div className="login-icon" aria-hidden="true">FI<span>.</span></div>
      <div className="eyebrow">ANALYST WORKSPACE</div>
      <h1>Welcome back</h1>
      <p className="muted">Sign in to review card payment risk and model insights.</p>
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      <label>
        Email
        <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
      </label>
      <label>
        Password
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </label>
      {error && <div className="error" role="alert">{error}</div>}
      <button type="submit" className="button" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
      <p className="login-help">Use an analyst, viewer or admin account created with the API CLI.</p>
    </form>
  );
}

function ServiceAccountNotice() {
  const { logout } = useAuth();
  return (
    <div className="card login" role="alert">
      <h1>Service account</h1>
      <p>
        This is a machine account (role <strong>service</strong>) used to submit transactions. It cannot read the
        analyst views; the API refuses those requests. Sign in with an analyst, viewer or admin account.
      </p>
      <button type="button" className="button" onClick={() => logout()}>
        Sign out
      </button>
    </div>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  const { session, ready } = useAuth();
  const content = !ready ? null : !session ? <Login /> : !CAN.readInsights(session.role) ? <ServiceAccountNotice /> : children;
  const centered = !session || !CAN.readInsights(session.role);
  return (
    <div className="app">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <Header />
      <main id="main" tabIndex={-1} className={centered ? "main main-login" : "main"}>
        {content}
      </main>
      <footer className="footer">© 2026 Fraud Intel. Sandbox: synthetic and replayed card data; decisions are simulated and never move money.</footer>
    </div>
  );
}
