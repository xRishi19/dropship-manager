"use client";

import { useMemo, useState } from "react";
import Badge from "@/components/Badge";
import { KeywordCandidate, percent } from "@/lib/api";

type Column = { key: keyof KeywordCandidate; label: string; numeric?: boolean; format?: (c: KeywordCandidate) => string };

const COLUMNS: Column[] = [
  { key: "keyword", label: "Keyword" },
  { key: "tier", label: "Tier" },
  { key: "score", label: "Score", numeric: true, format: (c) => c.score.toFixed(2) },
  { key: "explore_score", label: "Explore score", numeric: true, format: (c) => c.explore_score.toFixed(2) },
  { key: "total_units_sold", label: "Units", numeric: true },
  { key: "recent_units_sold", label: "Recent", numeric: true },
  { key: "distinct_products", label: "Products", numeric: true },
  { key: "successful_products", label: "Selling", numeric: true },
  { key: "sell_through_lift", label: "Lift", numeric: true, format: (c) => `${c.sell_through_lift.toFixed(2)}×` },
  { key: "largest_product_share", label: "Concentration", numeric: true, format: (c) => percent(c.largest_product_share) },
  { key: "trend", label: "Trend" },
];

export default function CandidateExplorer({ candidates }: { candidates: KeywordCandidate[] }) {
  const [sort, setSort] = useState<{ key: keyof KeywordCandidate; desc: boolean }>({ key: "score", desc: true });
  const [tier, setTier] = useState("");
  const [trend, setTrend] = useState("");
  const [text, setText] = useState("");
  const [minUnits, setMinUnits] = useState(0);

  const rows = useMemo(() => {
    const needle = text.trim().toLowerCase();
    const filtered = candidates.filter((c) => (!tier || c.tier === tier) && (!trend || c.trend === trend)
      && c.total_units_sold >= minUnits && (!needle || c.keyword.includes(needle)));
    return filtered.sort((a, b) => {
      const x = a[sort.key] ?? -Infinity;
      const y = b[sort.key] ?? -Infinity;
      const order = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
      return sort.desc ? -order : order;
    });
  }, [candidates, sort, tier, trend, text, minUnits]);

  const header = (column: Column) => {
    const active = sort.key === column.key;
    return (
      <th key={column.key} className={`sortable ${column.numeric ? "num" : ""}`}
        onClick={() => setSort({ key: column.key, desc: active ? !sort.desc : !!column.numeric })}>
        {column.label}{active ? (sort.desc ? " ↓" : " ↑") : ""}
      </th>
    );
  };

  return (
    <>
      <div className="toolbar">
        <input className="input" placeholder="Filter keywords" value={text} onChange={(e) => setText(e.target.value)} />
        <select className="input" value={tier} onChange={(e) => setTier(e.target.value)}>
          <option value="">Both tiers</option>
          <option value="proven">Proven</option>
          <option value="explore">Explore</option>
        </select>
        <select className="input" value={trend} onChange={(e) => setTrend(e.target.value)}>
          <option value="">Any trend</option>
          {["rising", "new_activity", "flat", "falling", "insufficient_data"].map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label className="muted small">Min units <input className="input" type="number" min={0} value={minUnits}
          onChange={(e) => setMinUnits(Number(e.target.value) || 0)} style={{ width: 70 }} /></label>
        <span className="muted small">{rows.length} of {candidates.length} candidates</span>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="data">
          <thead><tr>{COLUMNS.map(header)}</tr></thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.keyword}>
                {COLUMNS.map((column) => (
                  <td key={column.key} className={column.numeric ? "num" : ""}>
                    {column.key === "tier" ? <Badge value={c.tier === "proven" ? "Proven" : "Explore"} />
                      : column.key === "trend" ? <Badge value={c.trend} />
                        : column.format ? column.format(c) : String(c[column.key] ?? "—")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
