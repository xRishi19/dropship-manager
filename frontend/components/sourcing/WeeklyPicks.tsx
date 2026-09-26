"use client";

import { useState } from "react";
import Badge from "@/components/Badge";
import { percent, Week, WeeklyRow } from "@/lib/api";

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function num(value: unknown, digits = 2): string {
  if (typeof value !== "number") return "—";
  const text = value.toFixed(digits);
  return digits > 0 ? text.replace(/0+$/, "").replace(/\.$/, "") : text;
}

function Pick({ row, asins }: { row: WeeklyRow; asins?: number }) {
  const [open, setOpen] = useState(false);
  const s = row.stats;
  return (
    <div className={`pick ${row.tier.toLowerCase()}`} onClick={() => setOpen(!open)}>
      <div className="badges">
        <Badge value={row.tier} label={`${row.tier}${asins ? ` · ${asins} ASINs` : ""}`} />
        <Badge value={row.status} />
        {row.market && <Badge value={row.market} tone="purple" label={row.market === "new_category" ? "new category" : "new niche"} />}
        <Badge value={row.trend} />
      </div>
      <div className="kw">{row.keyword}</div>
      <div className="meta">
        <span>#{row.rank} in tier</span>
        <span>{row.total_units_sold} units</span>
        <span>{row.recent_units_sold} recent</span>
        <span>{row.distinct_products} products</span>
        <span>confidence {row.confidence === null ? "—" : row.confidence.toFixed(2)}</span>
      </div>
      {open && (
        <>
          <dl className="kv small" style={{ marginTop: 10 }}>
            <dt>Sell-through lift</dt><dd>{num(s.sell_through_lift)}× store average</dd>
            <dt>Concentration</dt><dd>{percent(s.largest_product_share as number | null)} of units from the top product</dd>
            <dt>Selling listings</dt><dd>{num(s.distinct_successful_listings, 0)} of {num(s.distinct_listings, 0)} listings sold</dd>
            <dt>Selling products</dt><dd>{num(s.successful_products, 0)} of {row.distinct_products} products</dd>
            <dt>Recent vs prior</dt><dd>{num(s.recent_units_sold, 0)} vs {num(s.prior_units_sold, 0)} units (28-day windows)</dd>
            {row.tier === "Explore" && (
              <><dt>Outside proven</dt><dd>{percent(s.outside_proven_share as number | null)} of listings mention no proven keyword</dd></>
            )}
          </dl>
          {s.examples && (
            <ul className="small muted" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
              {s.examples.slice(0, 3).map((e, index) => <li key={index}>{e.title} — {e.units} sold</li>)}
            </ul>
          )}
        </>
      )}
      <div className="reason">{row.reason}</div>
    </div>
  );
}

export default function WeeklyPicks({ week }: { week: Week }) {
  if (!week.week) return <div className="empty">No weekly picks yet. Run the weekly analysis to create them.</div>;
  const byDay = (day: string, tier: string) => week.results.find((r) => r.day === day && r.tier === tier);
  const legacy = week.results.some((r) => !r.tier);
  return (
    <>
      <div className="toolbar small muted">
        <span>Week of {week.week} · analysis through {week.as_of} · {week.model}</span>
        {week.selection_method === "fallback" && <Badge value="fallback" tone="amber" label="GPT failed: deterministic fallback" />}
        {week.week !== week.current_week && <Badge value="old" tone="amber" label={`not the current week (${week.current_week})`} />}
      </div>
      {legacy ? (
        <div className="notice">This week was saved before Proven/Explore tiers existed; run the weekly analysis to regenerate it.</div>
      ) : (
        <div className="days">
          {DAYS.map((day) => {
            const proven = byDay(day, "Proven");
            const explore = byDay(day, "Explore");
            return (
              <div className="day" key={day}>
                <div className="name">{day}</div>
                {proven ? <Pick key={proven.keyword} row={proven} asins={week.asins?.Proven} /> : <div />}
                {explore ? <Pick key={explore.keyword} row={explore} asins={week.asins?.Explore} /> : <div />}
              </div>
            );
          })}
        </div>
      )}
      <div className="toolbar small" style={{ marginTop: 14 }}>
        <span className="muted">Dropped since last week:</span>
        {week.dropped?.length ? week.dropped.map((k) => <Badge key={k} value="DROPPED" label={`${k} · DROPPED`} />)
          : <span className="muted">none</span>}
      </div>
      <div className="toolbar small">
        <span className="muted">Resting Explore keywords (cooldown):</span>
        {week.resting?.length ? week.resting.map((k) => <Badge key={k} value="resting" tone="amber" label={k} />)
          : <span className="muted">none</span>}
      </div>
    </>
  );
}
