"use client";

import { CAN, useAuth } from "@/lib/auth";

const yes = (b: boolean) => (b ? "Yes" : "No");

export default function SettingsPage() {
  const { session, logout } = useAuth();
  if (!session) return null;
  const r = session.role;
  return (
    <div className="page">
      <div className="page-header"><div><div className="eyebrow">ACCOUNT / SETTINGS</div><h1>Settings</h1><p>View your current workspace session.</p></div></div>
      <div className="section-heading"><div><h2>Session details</h2><p>Your access to this analyst workspace.</p></div></div>
      <div className="card settings-card">
        <table className="table kv">
          <tbody>
            <tr>
              <th scope="row">Signed in as</th>
              <td>{session.email}</td>
            </tr>
            <tr>
              <th scope="row">Role</th>
              <td>{r}</td>
            </tr>
            <tr>
              <th scope="row">Session expires</th>
              <td>{new Date(session.expiresAt).toLocaleString()} (you will be asked to sign in again)</td>
            </tr>
          </tbody>
        </table>
        <button type="button" className="button" onClick={() => logout()}>
          Sign out
        </button>
      </div>
      <div className="section-heading distribution-heading"><div><h2>What this role can do here</h2><p>The interface hides controls your role cannot use; the API enforces every permission regardless.</p></div></div>
      <div className="card settings-card">
        <table className="table kv">
          <tbody>
            <tr><th scope="row">View transactions, analytics, live feed</th><td>{yes(CAN.readInsights(r))}</td></tr>
            <tr><th scope="row">Read rules</th><td>{yes(CAN.readInsights(r))}</td></tr>
            <tr><th scope="row">Create rule versions</th><td>{yes(CAN.createRules(r))}</td></tr>
            <tr><th scope="row">Activate / deactivate rules</th><td>{yes(CAN.activateRules(r))}</td></tr>
            <tr><th scope="row">Read cases and label history</th><td>{yes(CAN.readInsights(r))}</td></tr>
            <tr><th scope="row">Open cases, add notes, record labels, work assigned cases</th><td>{yes(CAN.workCases(r))}</td></tr>
            <tr><th scope="row">Assign anyone, reopen closed cases</th><td>{yes(CAN.administerCases(r))}</td></tr>
            <tr><th scope="row">Stream health (backlog, dead letters)</th><td>{yes(CAN.streamStatus(r))}</td></tr>
            <tr><th scope="row">Score transactions</th><td>Not from this interface (service and admin API clients only)</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
