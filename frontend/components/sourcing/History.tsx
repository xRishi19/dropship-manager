"use client";

import Badge from "@/components/Badge";
import { HistoryEntry, Outcome } from "@/lib/api";

export default function History({ entries, outcomes, restingNow, cooldown }:
  { entries: HistoryEntry[]; outcomes: Outcome[]; restingNow: string[]; cooldown: number }) {
  const results = new Map<string, Outcome["keywords"][number]>();
  for (const week of outcomes) for (const k of week.keywords) results.set(`${week.week}|${k.keyword}`, k);
  if (!entries.length) return <div className="empty">No saved weeks yet.</div>;
  return (
    <>
      <p className="muted small">
        Explore keywords rest for {cooldown} weeks after they&apos;re picked while their tests run.
        Resting now: {restingNow.length ? restingNow.join(", ") : "none"}.
      </p>
      <table className="data">
        <thead>
          <tr>
            <th>Week</th><th>Day</th><th>Tier</th><th>Keyword</th><th>Status</th><th className="num">Units at pick</th>
            <th>Trend</th><th>Results since (new listings / sold / units)</th><th>Cooldown</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e, index) => {
            const result = results.get(`${e.week}|${e.keyword}`);
            return (
              <tr key={`${e.week}-${e.keyword}-${index}`}>
                <td>{e.week}</td>
                <td className="muted">{e.day ?? "—"}</td>
                <td>{e.tier ? <Badge value={e.tier} /> : <span className="faint">—</span>}</td>
                <td>{e.keyword}</td>
                <td><Badge value={e.status} /></td>
                <td className="num">{e.total_units_sold ?? "—"}</td>
                <td>{e.trend ? <Badge value={e.trend} /> : "—"}</td>
                <td className="small">
                  {!result ? <span className="faint">—</span>
                    : result.outcome === "no_data" ? <span className="muted">no matching new listings</span>
                      : `${result.new_listings} / ${result.new_listings_sold} / ${result.units_sold}`}
                </td>
                <td className="small">
                  {e.tier !== "Explore" || !e.cooldown ? <span className="faint">—</span>
                    : e.cooldown === "resting" ? <Badge value="resting" tone="amber" label={`resting · available from ${e.available_from}`} />
                      : e.cooldown === "picked this week" ? <Badge value="picked" tone="purple" label="picked this week" />
                        : <span className="muted">{e.cooldown}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
