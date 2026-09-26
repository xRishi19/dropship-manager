"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import FinanceChart, { Metric, METRICS } from "@/components/overview/FinanceChart";
import OrdersTable from "@/components/overview/OrdersTable";
import SummaryCards from "@/components/overview/SummaryCards";
import { api, OrderSummary, Overview, query, RangeKey, RANGES } from "@/lib/api";

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
const monthName = (key: string, withYear = false) =>
  new Date(Number(key.slice(0, 4)), Number(key.slice(5, 7)) - 1, 1)
    .toLocaleString("en-US", withYear ? { month: "long", year: "numeric" } : { month: "long" });

// January through the current month of this year, as "YYYY-MM".
const thisYearMonths = () => {
  const today = new Date();
  return Array.from({ length: today.getMonth() + 1 },
    (_, i) => `${today.getFullYear()}-${String(i + 1).padStart(2, "0")}`);
};

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

  useEffect(() => {
    const timer = setInterval(load, 60000); // Pick up the background sync's new orders.
    return () => clearInterval(timer);
  }, [load]);

  const isMonth = MONTH.test(range);
  const openMonth = (month: string) => {
    if (!month) return;
    if (!isMonth) setPrevious(range);
    setRange(month);
  };
  const previousLabel = RANGES.find((r) => r.key === previous)?.label ?? previous;

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
