"use client";

import { Bar, BarChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { money, SeriesPoint } from "@/lib/api";

export type Metric = "revenue" | "cost" | "profit";

export const METRICS: { key: Metric; label: string; color: string; field: keyof SeriesPoint }[] = [
  { key: "revenue", label: "Revenue", color: "var(--revenue)", field: "revenue_cents" },
  { key: "cost", label: "Amazon Cost", color: "var(--cost)", field: "cost_cents" },
  { key: "profit", label: "Profit", color: "var(--profit)", field: "profit_cents" },
];

type TooltipProps = { active?: boolean; payload?: { payload: SeriesPoint }[] };

/**
 * Render financial values and order counts for the chart’s hovered bucket.
 *
 * @param active - Whether Recharts considers the tooltip active.
 * @param payload - Optional chart payload array whose first entry contains the selected SeriesPoint.
 * @returns Tooltip React element, or null when inactive or lacking data.
 */
function ChartTooltip({ active, payload }: TooltipProps) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="chart-tooltip">
      <div className="t">{point.label}</div>
      <div><span className="swatch" style={{ background: "var(--revenue)" }} /> Revenue {money(point.revenue_cents)}</div>
      <div><span className="swatch" style={{ background: "var(--cost)" }} /> Amazon cost {money(point.cost_cents)}</div>
      <div className={point.profit_cents >= 0 ? "pos" : "neg"}>
        <span className="swatch" style={{ background: "var(--profit)" }} /> Profit {money(point.profit_cents)}
      </div>
      <div className="muted">{point.orders} order{point.orders === 1 ? "" : "s"}</div>
      {point.awaiting_cost > 0 && (
        <div className="muted small">{point.awaiting_cost} awaiting Amazon cost (in revenue, not in cost/profit)</div>
      )}
    </div>
  );
}

/**
 * Compactly format signed cents for chart-axis labels, abbreviating amounts of at least $1,000.
 *
 * @param cents - Signed monetary amount in cents.
 * @returns Rounded dollar label, with a Unicode minus for negatives and a k suffix for thousands.
 */
const axisMoney = (cents: number) => {
  const dollars = Math.abs(cents / 100);
  const sign = cents < 0 ? "−" : "";
  return dollars >= 1000 ? `${sign}$${(dollars / 1000).toFixed(1)}k` : `${sign}$${dollars.toFixed(0)}`;
};

type Props = { series: SeriesPoint[]; metrics: Set<Metric>; onSelectMonth?: (month: string) => void };

/**
 * Render selected revenue, Amazon-cost, and profit series as responsive grouped bars. Monthly buckets can invoke a callback to open their daily view.
 *
 * @param series - Financial buckets and labels supplied through props.
 * @param metrics - Set selecting which metric bars are visible.
 * @param onSelectMonth - Optional callback receiving a clicked bucket’s YYYY-MM month key.
 * @returns React element containing the responsive chart, axes, zero line, and tooltip.
 */
export default function FinanceChart({ series, metrics, onSelectMonth }: Props) {
  const shown = METRICS.filter((m) => metrics.has(m.key));
  // Monthly bars (12 Months / YTD) open that month's daily view.
  const clickable = !!onSelectMonth && series.some((p) => p.month);
  /**
   * Forward a clicked monthly chart bucket to the month-selection callback.
   *
   * @param bar - Recharts bar event data with an optional SeriesPoint payload.
   * @returns Nothing; invokes onSelectMonth only when a month exists in the payload.
   */
  const select = (bar: { payload?: SeriesPoint }) => {
    if (bar.payload?.month) onSelectMonth?.(bar.payload.month);
  };
  return (
    <div style={{ width: "100%", height: 320 }}>
      <ResponsiveContainer>
        <BarChart data={series} margin={{ top: 8, right: 8, left: 4, bottom: 0 }} barGap={2}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis dataKey="label" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false}
            axisLine={{ stroke: "var(--border)" }} interval="preserveStartEnd" minTickGap={16} />
          <YAxis tickFormatter={axisMoney} tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false}
            axisLine={false} width={56} />
          <ReferenceLine y={0} stroke="var(--border-strong)" />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(255,255,255,0.04)" }} />
          {shown.map((m) => (
            <Bar key={m.key} dataKey={m.field} name={m.label} fill={m.color} radius={[3, 3, 0, 0]} maxBarSize={28}
              onClick={clickable ? select : undefined} style={clickable ? { cursor: "pointer" } : undefined} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
