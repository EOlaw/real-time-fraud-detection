"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api";

// The token lives in sessionStorage: it survives a reload but not closing the tab, and it is
// discarded at its expiry time. Claims are read only for display and to hide controls the role
// cannot use; the API verifies the token and enforces every permission.
const KEY = "fraud.session";

export type Role = "admin" | "analyst" | "viewer" | "service" | "unknown";

interface Session {
  token: string;
  /** User id from the token's ``sub`` claim (display and "assign to me" only). */
  userId: string;
  email: string;
  role: Role;
  expiresAt: number;
}

interface AuthState {
  session: Session | null;
  ready: boolean;
  /** Why the last session ended (shown on the sign-in form), e.g. token expiry. */
  notice: string | null;
  login: (email: string, password: string) => Promise<void>;
  logout: (reason?: string) => void;
}

const AuthContext = createContext<AuthState | null>(null);

function claimsOf(token: string): { role: Role; userId: string } {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(part));
    const role = ["admin", "analyst", "viewer", "service"].includes(claims.role) ? claims.role : "unknown";
    return { role, userId: String(claims.sub ?? "") };
  } catch {
    return { role: "unknown", userId: "" };
  }
}

function load(): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    return s.expiresAt > Date.now() && s.userId ? s : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setSession(load());
    setReady(true);
  }, []);

  const logout = useCallback((reason?: string) => {
    try {
      sessionStorage.removeItem(KEY);
    } catch {}
    setNotice(reason ?? null);
    setSession(null);
  }, []);

  useEffect(() => {
    if (!session) return;
    const t = setTimeout(
      () => logout("Your session expired. Sign in again to continue."),
      session.expiresAt - Date.now(),
    );
    return () => clearTimeout(t);
  }, [session, logout]);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api.login(email, password);
    const s: Session = {
      token: res.access_token,
      email,
      ...claimsOf(res.access_token),
      expiresAt: Date.now() + res.expires_in * 1000,
    };
    try {
      sessionStorage.setItem(KEY, JSON.stringify(s));
    } catch {}
    setNotice(null);
    setSession(s);
  }, []);

  return (
    <AuthContext.Provider value={{ session, ready, notice, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

/** UI capabilities per role. These only hide controls; the API is the enforcement point. */
export const CAN = {
  readInsights: (r: Role) => r === "admin" || r === "analyst" || r === "viewer",
  createRules: (r: Role) => r === "admin" || r === "analyst",
  activateRules: (r: Role) => r === "admin",
  streamStatus: (r: Role) => r === "admin",
  workCases: (r: Role) => r === "admin" || r === "analyst",
  administerCases: (r: Role) => r === "admin",
};
