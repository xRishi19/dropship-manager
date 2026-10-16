"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import FinanceChart, { Metric, METRICS } from "@/components/overview/FinanceChart";
import OrdersTable from "@/components/overview/OrdersTable";
import SummaryCards from "@/components/overview/SummaryCards";
import { api, OrderSummary, Overview, query, RangeKey, RANGES } from "@/lib/api";
import { useSyncRefresh } from "@/lib/sync";

const STATUS_FILTERS = [
  { value: "", label: "All statuses" },
  { value: "Completed", label: "Completed" },
  { value: "Return pending", label: "Return pending" },
  { value: "Returned", label: "Returned" },
  { value: "Refunded", label: "Refunded" },
  { value: "Cancelled", label: "Cancelled" },
];
const MATCH_FILTERS = [
  { value: "", label: "All Amazon matches" },
  { value: "COST_UNKNOWN", label: "Amazon cost unknown" },
  { value: "NEEDS_REVIEW", label: "Needs review" },
  { value: "MATCHED,MANUAL", label: "Matched / manual" },
];
const PAGE = 100;
const MONTH = /^\d{4}-\d{2}$/;

// "2026-03" -> "March" (or "March 2026" with the year).
/**
 * Convert a calendar-month key to a readable month label using local calendar construction.
 *
 * @param key - Calendar month in YYYY-MM form.
 * @param withYear - Whether to include the year; defaults to false.
 * @returns Localized full month name, optionally followed by the year.
 */
const monthName = (key: string, withYear = false) =>
  new Date(Number(key.slice(0, 4)), Number(key.slice(5, 7)) - 1, 1)
    .toLocaleString("en-US", withYear ? { month: "long", year: "numeric" } : { month: "long" });

// January through the current month of this year, as "YYYY-MM".
/**
 * List every calendar month from January through the current browser-local month.
 *
 * @remarks Takes no arguments.
 * @returns Array of YYYY-MM keys in chronological order for the current year.
 */
const thisYearMonths = () => {
  const today = new Date();
  return Array.from({ length: today.getMonth() + 1 },
    (_, i) => `${today.getFullYear()}-${String(i + 1).padStart(2, "0")}`);
};

/**
 * Render financial summaries, selectable chart metrics, order filters, exports, and expandable orders. Loads data after filter changes and completed syncs, ignoring stale request responses.
 *
 * @remarks Takes no arguments.
 * @returns React fragment containing the dashboard controls, chart, summaries, orders, and request errors.
 */
export default function OverviewPage() {
  const [range, setRange] = useState<RangeKey>("30d");
  const [previous, setPrevious] = useState<RangeKey>("30d"); // The preset a month view goes back to.
  const [metrics, setMetrics] = useState<Set<Metric>>(new Set(["revenue", "cost", "profit"]));
  const [overview, setOverview] = useState<Overview | null>(null);
  const [orders, setOrders] = useState<OrderSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(PAGE);
  const [status, setStatus] = useState("");
  const [match, setMatch] = useState("");
  const [search, setSearch] = useState("");
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [ordersError, setOrdersError] = useState<string | null>(null);
  const requests = useRef({ overview: 0, orders: 0 });

  const filters = { range, status, match_status: match, q: search.trim() };

  /**
   * Start concurrent overview and order requests for the current filters. Apply only the latest responses of each kind and record their errors independently.
   *
   * @remarks Takes no arguments.
   * @returns Nothing; schedules asynchronous requests that update dashboard state.
   */
  const load = useCallback(() => {
    // Only the newest response of each kind is applied, so a slow older request can't overwrite it.
    const overviewId = ++requests.current.overview;
    const ordersId = ++requests.current.orders;
    api<Overview>(`/overview${query({ range })}`)
      .then((r) => { if (overviewId === requests.current.overview) { setOverview(r); setOverviewError(null); } })
      .catch((e: Error) => { if (overviewId === requests.current.overview) setOverviewError(e.message); });
    api<{ total: number; orders: OrderSummary[] }>(`/orders${query({ range, status, match_status: match, q: search.trim(), limit })}`)
      .then((r) => {
        if (ordersId !== requests.current.orders) return;
        setOrders(r.orders);
        setTotal(r.total);
        setOrdersError(null);
      })
      .catch((e: Error) => { if (ordersId === requests.current.orders) setOrdersError(e.message); });
  }, [range, status, match, search, limit]);

  useEffect(() => {
    const timer = setTimeout(load, 200);
    return () => clearTimeout(timer);
  }, [load]);

  useSyncRefresh(load); // Pick up new orders and Amazon costs after every sync.

  const isMonth = MONTH.test(range);
  /**
   * Switch to a month’s daily chart while retaining the preset to return to. Ignore an empty month selection.
   *
   * @param month - Calendar month key in YYYY-MM form, or an empty selection.
   * @returns Nothing; updates range and possibly previous-range state.
   */
  const openMonth = (month: string) => {
    if (!month) return;
    if (!isMonth) setPrevious(range);
    setRange(month);
  };
  const previousLabel = RANGES.find((r) => r.key === previous)?.label ?? previous;

  /**
   * Toggle one chart metric without mutating the existing selection set.
   *
   * @param metric - Revenue, cost, or profit series to show or hide.
   * @returns Nothing; replaces the selected-metrics state.
   */
  const toggle = (metric: Metric) => {
    const next = new Set(metrics);
    if (next.has(metric)) next.delete(metric);
    else next.add(metric);
    setMetrics(next);
  };

  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <div className="seg">
          {RANGES.map((r) => (
            <button key={r.key} className={range === r.key ? "on" : ""} onClick={() => setRange(r.key)}>{r.label}</button>
          ))}
        </div>
        <select className="input" value={isMonth ? range : ""} onChange={(e) => openMonth(e.target.value)}>
          <option value="">Month…</option>
          {thisYearMonths().map((m) => <option key={m} value={m}>{monthName(m)}</option>)}
          {isMonth && !thisYearMonths().includes(range) && <option value={range}>{monthName(range, true)}</option>}
        </select>
        <span className="spacer" style={{ flex: 1 }} />
        <a className="btn" href={`/api/orders/export${query({ scope: "filtered", ...filters })}`} download>Export current view</a>
        <a className="btn" href="/api/orders/export?scope=all" download>Export all</a>
      </div>

      {overviewError && <p className="error">Dashboard: {overviewError}</p>}
      {ordersError && <p className="error">Orders: {ordersError}</p>}
      {overview && <SummaryCards cards={overview.cards} />}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="toolbar">
          {isMonth && <button className="btn small" onClick={() => setRange(previous)}>← Back to {previousLabel}</button>}
          <h2 style={{ margin: 0 }}>{isMonth ? monthName(range, true) : "Revenue, cost & profit"}</h2>
          <span className="muted small">{overview ? `by ${overview.granularity} · ${overview.timezone}` : ""}</span>
          <span style={{ flex: 1 }} />
          <div className="checks">
            {METRICS.map((m) => (
              <label key={m.key}>
                <input type="checkbox" checked={metrics.has(m.key)} onChange={() => toggle(m.key)} />
                <span className="swatch" style={{ background: m.color }} /> {m.label}
              </label>
            ))}
          </div>
        </div>
        {overview && <FinanceChart series={overview.series} metrics={metrics} onSelectMonth={openMonth} />}
      </div>

      <div className="card">
        <div className="toolbar">
          <h2 style={{ margin: 0 }}>Orders</h2>
          <span className="muted small">{total} in view</span>
          <span style={{ flex: 1 }} />
          <input className="input" placeholder="Search title, item, order, buyer" value={search}
            onChange={(e) => setSearch(e.target.value)} style={{ width: 260 }} />
          <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUS_FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
          <select className="input" value={match} onChange={(e) => setMatch(e.target.value)}>
            {MATCH_FILTERS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
        <OrdersTable orders={orders} onChanged={load} />
        {total > orders.length && (
          <div style={{ textAlign: "center", marginTop: 12 }}>
            <button className="btn" onClick={() => setLimit(limit + PAGE)}>Show more ({total - orders.length} remaining)</button>
          </div>
        )}
      </div>
    </>
  );
}
