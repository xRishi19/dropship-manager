import { Cards, money, percent } from "@/lib/api";

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "pos" | "neg" }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
      <div className="sub" style={{ whiteSpace: "pre-line" }}>{sub}</div>
    </div>
  );
}

export default function SummaryCards({ cards }: { cards: Cards }) {
  const excluded = cards.excluded.cancelled + cards.excluded.returned + cards.excluded.refunded;
  const tone = (cents: number | null) => (cents === null ? undefined : cents >= 0 ? "pos" : "neg");
  const awaiting = cards.awaiting_cost ? `${cards.awaiting_cost} awaiting Amazon cost` : "All costs known";
  return (
    <div className="cards">
      <Stat label="Total Orders" value={String(cards.total_orders)}
        sub={excluded ? `+${excluded} cancelled/returned (excluded)` : undefined} />
      <Stat label="Net Revenue" value={money(cards.net_revenue_cents)}
        sub={cards.revenue_estimated ? `${cards.revenue_estimated} estimated until eBay posts payout` : "After eBay fees, ads & tax"} />
      <Stat label="Amazon Cost" value={money(cards.amazon_cost_cents)} sub={awaiting} />
      <Stat label="Profit" value={money(cards.profit_after_expenses_cents)} tone={tone(cards.profit_after_expenses_cents)}
        sub={`Before expenses: ${money(cards.profit_cents)}`
          + (cards.ordering_fees_cents ? ` (after ${money(cards.ordering_fees_cents)} ordering fees)` : "")
          + `\nExpenses: ${money(cards.expenses_cents)}`
          + (cards.awaiting_cost ? "\nOrders with known cost only" : "")} />
      <Stat label="Avg Profit / Order" value={money(cards.avg_profit_per_order_cents)}
        tone={tone(cards.avg_profit_per_order_cents)} />
      <Stat label="Profit Margin" value={percent(cards.profit_margin)}
        tone={cards.profit_margin === null ? undefined : cards.profit_margin >= 0 ? "pos" : "neg"}
        sub={cards.needs_review ? `${cards.needs_review} matches need review` : undefined} />
      <Stat label="Prime Cashback" value={money(cards.prime_cashback_cents)} tone="pos"
        sub={`${+(cards.prime_cashback_rate * 100).toFixed(2)}% of Amazon cost · not in profit`} />
    </div>
  );
}
