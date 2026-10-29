"use client";

import { useState } from "react";
import { Empty, ErrorState, Loading } from "@/components/States";
import { api, ApiError, type RuleAction, type RuleVersion } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";

const ACTIONS: { value: RuleAction; help: string }[] = [
  { value: "flag", help: "record a hit only; never changes the decision" },
  { value: "challenge", help: "at least challenge" },
  { value: "review", help: "at least review" },
  { value: "decline", help: "decline" },
];

const EXAMPLE = JSON.stringify(
  {
    all: [
      { field: "channel", op: "eq", value: "card_not_present" },
      { field: "amount", op: "gte", value: 900 },
    ],
  },
  null,
  2,
);

interface Draft {
  rule_key: string;
  name: string;
  description: string;
  priority: string;
  action: RuleAction;
  condition: string;
}

const EMPTY_DRAFT: Draft = { rule_key: "", name: "", description: "", priority: "100", action: "review", condition: EXAMPLE };

function draftFrom(v: RuleVersion): Draft {
  return {
    rule_key: v.rule_key,
    name: v.name,
    description: v.description ?? "",
    priority: String(v.priority),
    action: v.action,
    condition: JSON.stringify(v.condition, null, 2),
  };
}

function CreateForm({ initial, onCreated, onCancel }: { initial: Draft; onCreated: (key: string) => void; onCancel: () => void }) {
  const { session } = useAuth();
  const [d, setD] = useState<Draft>(initial);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setD({ ...d, [k]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    let condition: unknown;
    try {
      condition = JSON.parse(d.condition);
    } catch {
      setErrors(["Condition is not valid JSON."]);
      return;
    }
    setBusy(true);
    setErrors([]);
    try {
      const v = await api.createRule(session!.token, {
        rule_key: d.rule_key.trim(),
        name: d.name.trim(),
        description: d.description.trim() || null,
        priority: Number(d.priority),
        action: d.action,
        condition,
      });
      onCreated(v.rule_key);
    } catch (err) {
      if (err instanceof ApiError && Array.isArray(err.details)) {
        setErrors(
          err.details.map((x: { path?: string; loc?: unknown[]; message?: string; msg?: string }) =>
            `${x.path ?? (x.loc ?? []).join(".")}: ${x.message ?? x.msg}`,
          ),
        );
      } else setErrors([err instanceof ApiError ? err.message : "Cannot reach the API"]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card rule-form" onSubmit={submit} aria-labelledby="create-h">
      <h2 id="create-h" className="card-title">
        New rule version (created inactive)
      </h2>
      <div className="form-grid">
        <label>
          Rule key
          <input value={d.rule_key} onChange={set("rule_key")} required pattern="[a-z0-9][a-z0-9_.\-]{0,63}" aria-describedby="key-help" />
          <span id="key-help" className="hint">Lowercase letters, digits, _ . -. An existing key gets the next version.</span>
        </label>
        <label>
          Name
          <input value={d.name} onChange={set("name")} required maxLength={200} />
        </label>
        <label>
          Priority (0–1000, higher first)
          <input type="number" min={0} max={1000} value={d.priority} onChange={set("priority")} required />
        </label>
        <label>
          Action
          <select value={d.action} onChange={set("action")}>
            {ACTIONS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.value}: {a.help}
              </option>
            ))}
          </select>
        </label>
        <label className="span-2">
          Description (optional)
          <input value={d.description} onChange={set("description")} maxLength={1000} />
        </label>
        <label className="span-2">
          Condition (JSON)
          <textarea value={d.condition} onChange={set("condition")} rows={9} spellCheck={false} className="mono" required />
        </label>
      </div>
      {errors.length > 0 && (
        <ul className="error form-errors" role="alert">
          {errors.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
      <div className="button-row">
        <button type="submit" className="button" disabled={busy}>
          {busy ? "Creating…" : "Create version"}
        </button>
        <button type="button" className="button ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RuleDetailPanel({ ruleKey, onChanged, onNewVersion }: { ruleKey: string; onChanged: () => void; onNewVersion: (v: RuleVersion) => void }) {
  const { session } = useAuth();
  const role = session!.role;
  const detail = useApi((t) => api.rule(t, ruleKey), [ruleKey]);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(fn: () => Promise<{ changed: boolean; active_version: number | null; previous_version: number | null }>) {
    setBusy(true);
    setMessage(null);
    try {
      const r = await fn();
      setMessage({
        text: r.changed
          ? `Active version: ${r.active_version ?? "none"} (was ${r.previous_version ?? "none"}). Audited.`
          : "No change: that state was already in effect.",
        error: false,
      });
      detail.reload();
      onChanged();
    } catch (e) {
      setMessage({ text: e instanceof ApiError ? `${e.message} (${e.code})` : "Cannot reach the API", error: true });
      detail.reload();
    } finally {
      setBusy(false);
    }
  }

  if (detail.error) return <ErrorState error={detail.error} what="Rule" />;
  if (!detail.data) return <Loading />;
  const d = detail.data;
  const current = d.active_version;
  return (
    <section className="card" aria-labelledby="detail-h">
      <h2 id="detail-h" className="card-title">
        <span className="mono">{d.rule_key}</span>: {current ? `version ${current} active` : "inactive"}
      </h2>
      {message && (
        <p className={message.error ? "error" : "notice"} role={message.error ? "alert" : "status"}>
          {message.text}
        </p>
      )}
      {CAN.activateRules(role) && current !== null && (
        <button
          type="button"
          className="button ghost"
          disabled={busy}
          onClick={() => act(() => api.deactivateRule(session!.token, d.rule_key, current))}
        >
          Deactivate version {current}
        </button>
      )}
      <ol className="versions">
        {d.versions.map((v) => (
          <li key={v.id} className={v.active ? "active" : undefined}>
            <div className="version-head">
              <strong>v{v.version}</strong> {v.active && <span className="badge allow">active</span>}{" "}
              <span>{v.name}</span>
              <span className="muted">
                {" "}
                · {v.action} · priority {v.priority} · created {fmtTime(v.created_at)}
              </span>
            </div>
            {v.description && <p className="muted">{v.description}</p>}
            <pre className="condition" aria-label={`Condition of version ${v.version}`}>
              {JSON.stringify(v.condition, null, 2)}
            </pre>
            <div className="button-row">
              {CAN.activateRules(role) && !v.active && (
                <button
                  type="button"
                  className="button ghost"
                  disabled={busy}
                  onClick={() => act(() => api.activateRule(session!.token, d.rule_key, v.version, current ?? "none"))}
                >
                  Activate v{v.version}
                </button>
              )}
              {CAN.createRules(role) && (
                <button type="button" className="button ghost" onClick={() => onNewVersion(v)}>
                  New version from v{v.version}
                </button>
              )}
              <span className="muted mono" title="SHA-256 of the canonical condition JSON">
                {v.condition_sha256.slice(0, 12)}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export default function RulesPage() {
  const { session } = useAuth();
  const role = session!.role;
  const list = useApi((t) => api.rules(t), []);
  const grammar = useApi((t) => api.grammar(t), []);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);

  return (
    <div className="page wide">
      <div className="page-header">
        <div>
          <div className="eyebrow">DECISIONING / RULES</div>
          <h1>Rules</h1>
          <p>
            Tenant rules run after the model-bound policy. They can only make a decision stricter, never change the
            model&apos;s fraud probability, and every version is immutable once created.
          </p>
        </div>
      </div>
      <p className="role-note">
        Signed in as <strong>{role}</strong>:{" "}
        {CAN.activateRules(role)
          ? "you can create versions and activate or deactivate them."
          : CAN.createRules(role)
            ? "you can create versions; an admin activates them."
            : "read-only. Analysts create versions; admins activate them."}{" "}
        <span className="muted">The API enforces these permissions; hidden buttons are a convenience only.</span>
      </p>

      {draft && (
        <CreateForm
          key={draft.rule_key + draft.condition}
          initial={draft}
          onCancel={() => setDraft(null)}
          onCreated={(k) => {
            setDraft(null);
            setSelected(k);
            list.reload();
          }}
        />
      )}

      <div className="split">
        <section className="card" aria-labelledby="list-h">
          <div className="card-head">
            <h2 id="list-h" className="card-title">
              Rules {list.data ? `(${list.data.active_count} of max ${list.data.max_active} active)` : ""}
            </h2>
            {CAN.createRules(role) && !draft && (
              <button type="button" className="button ghost" onClick={() => setDraft(EMPTY_DRAFT)}>
                New rule
              </button>
            )}
          </div>
          {list.error && <ErrorState error={list.error} what="Rules" />}
          {list.loading && !list.data && <Loading />}
          {list.data && list.data.items.length === 0 && (
            <Empty>No rules yet. Without active rules, final decisions equal the policy decisions.</Empty>
          )}
          {list.data && list.data.items.length > 0 && (
            <div className="table-scroll" role="region" aria-label="Rules table" tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Rule key</th>
                    <th scope="col">Active</th>
                    <th scope="col">Action</th>
                    <th scope="col">Priority</th>
                    <th scope="col">Versions</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.items.map((r) => (
                    <tr key={r.rule_key} className={r.rule_key === selected ? "selected" : undefined}>
                      <td>
                        <button type="button" className="link mono" onClick={() => setSelected(r.rule_key)}>
                          {r.rule_key}
                        </button>
                      </td>
                      <td>{r.active_version ? `v${r.active_version}` : <span className="muted">inactive</span>}</td>
                      <td>{r.active?.action ?? "—"}</td>
                      <td>{r.active?.priority ?? "—"}</td>
                      <td>
                        {r.versions} (latest v{r.latest_version})
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <div>
          {selected ? (
            <RuleDetailPanel
              ruleKey={selected}
              onChanged={list.reload}
              onNewVersion={(v) => {
                setDraft(draftFrom(v));
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          ) : (
            <section className="card">
              <Empty>Select a rule to inspect its versions.</Empty>
            </section>
          )}
        </div>
      </div>

      <details className="card grammar">
        <summary>Condition grammar and allowed fields</summary>
        {grammar.error && <ErrorState error={grammar.error} what="Grammar" />}
        {grammar.data && (
          <>
            <p>
              Combinators: <code>all</code>, <code>any</code>, <code>not</code>. Predicates:{" "}
              <code>{'{"field", "op", "value"}'}</code>. {grammar.data.logic}. Limits:{" "}
              {Object.entries(grammar.data.limits)
                .map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`)
                .join(", ")}
              .
            </p>
            <div className="table-scroll" role="region" aria-label="Grammar fields table" tabIndex={0}>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    <th scope="col">Type</th>
                    <th scope="col">Operators</th>
                    <th scope="col">Description</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(grammar.data.fields).map(([name, f]) => (
                    <tr key={name}>
                      <td className="mono">{name}</td>
                      <td>
                        {f.type}
                        {f.nullable ? ", nullable" : ""}
                      </td>
                      <td className="mono">
                        {(grammar.data!.operators[f.type] ?? [])
                          .filter((o) => f.nullable || !o.startsWith("is_null"))
                          .map((o) => o.replace(" (nullable fields)", ""))
                          .join(" ")}
                      </td>
                      <td className="wrap">
                        {f.description}
                        {f.values ? `: ${f.values.join(", ")}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </details>
    </div>
  );
}
