"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { DecisionTrace } from "@/components/DecisionTrace";
import { ExplanationState } from "@/components/ExplanationState";
import { Gauge } from "@/components/Gauge";
import { Histogram } from "@/components/Histogram";
import { ShapBars } from "@/components/ShapBars";
import { ErrorState, Loading } from "@/components/States";
import { type SortKey, TransactionTable } from "@/components/TransactionTable";
import { api, ApiError, type AssessmentRow, type Distribution, type PredictionDetail, type Source } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { useApi } from "@/lib/hooks";
import { binIndex, CATEGORY_CODES, escalated, policyZones } from "@/lib/format";

const PAGE = 50;
// Display order mirrors the layout: top row, then customer-velocity row.
const HIST_ORDER = [
  "hour_of_day",
  "category",
  "amount",
  "cust_txn_count_7d",
  "cust_txn_count_30d",
  "cust_same_category_count_30d",
];

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function markerFor(d: Distribution, row: AssessmentRow | null, detail: PredictionDetail | null): number | null {
  if (!row) return null;
  if (d.feature === "category") return d.bins.indexOf(row.category);
  const v = d.feature === "amount" ? row.amount : detail?.features?.[d.feature];
  return typeof v === "number" && d.edges ? binIndex(d.edges, v) : null;
}

export default function DashboardPage() {
  const { session, logout } = useAuth();
  const token = session!.token;
  const [search, setSearch] = useState("");
  const q = useDebounced(search.trim(), 250);
  const [sort, setSort] = useState<SortKey>("scored_at");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [source, setSource] = useState<Source | "all">("all");
  const [rows, setRows] = useState<AssessmentRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const router = useRouter();
  const [caseError, setCaseError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);

  async function openCase(assessmentId: string) {
    setOpening(true);
    setCaseError(null);
    try {
      const res = await api.openCase(token, assessmentId);
      router.push(`/cases/${res.case.id}`);
    } catch (e) {
      setCaseError(e instanceof ApiError ? e.message : "Cannot reach the API");
      setOpening(false);
    }
  }

  const loadPage = (offset: number) => {
    setLoading(true);
    api
      .assessments(token, { q, sort, order, limit: PAGE, offset, sources: source === "all" ? [] : [source] })
      .then((page) => {
        setRows((prev) => (offset === 0 ? page.items : [...prev, ...page.items]));
        setTotal(page.total);
        setListError(null);
        if (offset === 0) setSelected((s) => (page.items.some((r) => r.assessment_id === s) ? s : (page.items[0]?.assessment_id ?? null)));
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) logout("Your session is no longer valid. Sign in again.");
        setListError(e instanceof ApiError ? e.message : "Cannot reach the API");
      })
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => loadPage(0), [token, q, sort, order, source]);

  const row = useMemo(() => rows.find((r) => r.assessment_id === selected) ?? null, [rows, selected]);

  const dist = useApi((t) => api.distributions(t), []);
  const detail = useApi(selected ? (t) => api.prediction(t, selected) : null, [selected]);
  const explain = useApi(
    selected && row?.status === "scored" ? (t) => api.explanation(t, selected) : null,
    [selected, row?.status],
  );
  // The customer's previous scored transaction, for the score delta.
  const history = useApi(
    row ? (t) => api.assessments(t, { q: row.customer_id, sort: "event_time", order: "desc", limit: 200 }) : null,
    [row?.assessment_id],
  );
  const previous = useMemo(() => {
    if (!row || !history.data) return null;
    const prior = history.data.items.find(
      (r) => r.customer_id === row.customer_id && r.event_time < row.event_time && r.risk_score !== null,
    );
    return prior?.risk_score ?? null;
  }, [row, history.data]);

  const zones = dist.data ? policyZones(dist.data.thresholds) : [];
  const histograms = HIST_ORDER.map((f) => dist.data?.distributions.find((d) => d.feature === f)).filter(
    (d): d is Distribution => !!d,
  );

  function onSort(k: SortKey) {
    if (k === sort) setOrder(order === "desc" ? "asc" : "desc");
    else {
      setSort(k);
      setOrder("desc");
    }
  }

  return (
    <div className="dashboard">
      <h1 className="sr-only">Transaction dashboard</h1>
      <section className="top">
        <div className="card gauge-card">
          <div className="card-title">Fraud Score</div>
          <Gauge value={row?.risk_score ?? null} previous={previous} zones={zones} emptyText={row ? "n/a" : "—"} />
          <div className="gauge-note">
            {row?.status === "degraded"
              ? "Scoring failed; sent to manual review"
              : row && escalated(row)
                ? "Model probability shown; a rule escalated the decision"
                : previous !== null
                  ? "Δ vs. customer's previous transaction"
                  : row
                    ? "First scored transaction for this customer"
                    : ""}
          </div>
        </div>
        <div className="card gauge-card">
          <div className="card-title">Anomaly Score</div>
          <Gauge value={null} zones={[]} disabled emptyText="—" />
          <div className="gauge-note">
            Offline-only baseline: not served by the API and excluded from fraud decisions.
          </div>
        </div>
        <div className="hist-grid" aria-label="Portfolio feature distributions">
          {histograms.map((d) => (
            <Histogram
              key={d.feature}
              title={d.title}
              labels={d.feature === "category" ? d.bins.map((b) => CATEGORY_CODES[b] ?? b) : d.bins}
              titles={d.bins}
              shares={d.shares}
              marker={markerFor(d, row, detail.data)}
            />
          ))}
          {dist.loading && !dist.data && <div className="card error-card"><Loading label="Loading distributions…" /></div>}
          {dist.error && <div className="card error-card">Distributions unavailable: {dist.error.message}</div>}
        </div>
      </section>
      <section className="bottom">
        <div className="panel">
          <h2 className="section-label">List of Transactions</h2>
          {listError && (
            <div className="error" role="alert">
              {listError}
            </div>
          )}
          <TransactionTable
            rows={rows}
            total={total}
            selected={selected}
            onSelect={setSelected}
            sort={sort}
            order={order}
            onSort={onSort}
            search={search}
            onSearch={setSearch}
            source={source}
            onSource={setSource}
            onMore={rows.length < total ? () => loadPage(rows.length) : null}
            loading={loading}
            error={listError}
          />
        </div>
        <div className="panel">
          <h2 className="section-label">Insights</h2>
          <div className="card insights" role="region" aria-label="Decision trace and explanation" tabIndex={0}>
            <div className="card-head">
              <div className="card-title">Decision trace</div>
              {row && CAN.workCases(session!.role) && (
                <button type="button" className="button ghost" disabled={opening} onClick={() => openCase(row.assessment_id)}
                  title="Opens the investigation case for this transaction (or the existing one)">
                  {opening ? "Opening…" : "Open case"}
                </button>
              )}
            </div>
            {caseError && <p className="error" role="alert">{caseError}</p>}
            {!row && <p className="muted">Select a transaction.</p>}
            {row && detail.loading && !detail.data && <Loading />}
            {detail.error && <ErrorState error={detail.error} what="Assessment detail" />}
            {row && detail.data && detail.data.assessment_id === row.assessment_id && <DecisionTrace d={detail.data} />}
            <div className="card-title spaced">Important Factors (Normalized SHAP)</div>
            {row?.status === "degraded" && <p className="muted">Degraded assessment: there is no model score to explain.</p>}
            {row?.status === "scored" && explain.loading && !explain.data && <Loading />}
            {explain.error && <ExplanationState error={explain.error} />}
            {explain.data && row && (
              <>
                <ShapBars contributions={explain.data.contributions} amount={row.amount} />
                <p className="footnote">
                  Bars are TreeSHAP contributions in log-odds to the model probability, scaled to the largest. Crimson
                  pushes toward fraud, teal toward legitimate. Model attribution, not causal evidence; rules are not part of
                  this explanation.
                  {detail.data?.model_version && ` Model ${detail.data.model_name}@${detail.data.model_version}.`}
                </p>
              </>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
