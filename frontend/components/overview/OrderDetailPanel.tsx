"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { api, dateTime, Email, money, OrderDetail, percent } from "@/lib/api";

const RETURN_OPTIONS = ["NONE", "RETURN_STARTED", "RETURN_COMPLETED", "RETURN_CANCELLED", "REFUNDED"];

export default function OrderDetailPanel({ orderId, onChanged }: { orderId: string; onChanged: () => void }) {
  const [order, setOrder] = useState<OrderDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cost, setCost] = useState("");
  const [emails, setEmails] = useState<Email[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<OrderDetail>(`/orders/${encodeURIComponent(orderId)}`).then(setOrder).catch((e: Error) => setError(e.message));
  }, [orderId]);
  useEffect(load, [load]);

  const loadEmails = useCallback(() =>
    api<Email[]>(`/orders/${encodeURIComponent(orderId)}/emails`).then(setEmails).catch((e: Error) => setError(e.message)),
  [orderId]);

  /** Runs an order action; resolves true on success (errors are shown in the panel). */
  const act = async (path: string, init: RequestInit): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      setOrder(await api<OrderDetail>(`/orders/${encodeURIComponent(orderId)}${path}`, init));
      onChanged();
      if (emails) loadEmails();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!order) return <div className="muted small">{error ?? "Loading…"}</div>;

  const saveCost = (value: string | null) =>
    act("/amazon-cost", { method: "POST", body: JSON.stringify({ amount: value }) }).then((ok) => { if (ok) setCost(""); });
  const link = (id: string, replace = false) =>
    act("/match", { method: "POST", body: JSON.stringify({ gmail_message_id: id, replace }) });
  const locked = order.match_status === "MATCHED" || order.match_status === "MANUAL";
  const signedMinutes = (m: number) => {
    const sign = m >= 0 ? "+" : "−";
    const days = Math.abs(m) / 1440;  // Amazon history imports have dates only.
    return Number.isInteger(days) && days >= 1 ? `${sign}${days} day${days === 1 ? "" : "s"}` : `${sign}${Math.abs(Math.round(m))} min`;
  };

  const fees = Object.entries(order.other_fees ?? {});
  const profitTone = (cents: number | null) => (cents === null ? "" : cents >= 0 ? "pos" : "neg");

  return (
    <div className="detail">
      {error && <div className="error wide">{error}</div>}
      <section>
        <h3>Order</h3>
        <dl className="kv">
          <dt>Product</dt>
          <dd>{order.items.map((i) => <div key={i.line_item_id}>{i.title}</div>)}</dd>
          <dt>Quantity</dt><dd>{order.quantity}</dd>
          <dt>ASIN</dt><dd>{order.asin ?? "—"}</dd>
          <dt>eBay Item ID</dt>
          <dd>{order.items.map((i) => (
            <div key={i.line_item_id}><a href={i.ebay_item_url} target="_blank" rel="noreferrer">{i.item_id}</a></div>
          ))}</dd>
          <dt>eBay Order ID</dt>
          <dd><a href={order.ebay_order_url} target="_blank" rel="noreferrer">{order.order_id}</a></dd>
          <dt>Category</dt><dd>{order.category ?? "—"}</dd>
          <dt>Sold</dt><dd>{dateTime(order.created_at)}</dd>
          <dt>eBay status</dt>
          <dd>{order.display_status === "Cancelled" ? "Cancelled" : order.fulfillment_status ?? "—"} · {order.payment_status ?? "—"}</dd>
          <dt>Return</dt><dd><Badge value={order.return_status} /></dd>
          <dt>Amazon match</dt>
          <dd>
            <Badge value={order.match_status} />{" "}
            {order.match_confidence !== null && <span className="muted small">score {order.match_confidence.toFixed(0)}/100</span>}
          </dd>
        </dl>
      </section>

      <section>
        <h3>Financials {order.revenue_estimated && <Badge value="estimated" tone="amber" />}</h3>
        <dl className="kv">
          <dt>Gross eBay total</dt><dd className="num">{money(order.gross_total_cents)}</dd>
          <dt>eBay sales tax</dt><dd className="num">{money(order.sales_tax_cents)}</dd>
          <dt>Final value fee</dt><dd className="num">{money(order.final_value_fee_cents)}</dd>
          <dt>Promoted/ad fee</dt><dd className="num">{money(order.ad_fee_cents)}</dd>
          {fees.map(([name, cents]) => (
            <Fragment key={name}><dt>{name.toLowerCase().replaceAll("_", " ")}</dt><dd className="num">{money(cents)}</dd></Fragment>
          ))}
          {!!order.refund_cents && <><dt>Refunds</dt><dd className="num neg">−{money(order.refund_cents)}</dd></>}
          <dt>Net eBay revenue</dt><dd className="num">{money(order.net_revenue_cents)}</dd>
          <dt>Amazon cost</dt>
          <dd className="num">{order.amazon_cost_cents === null ? <span className="neg">Unknown</span> : money(order.amazon_cost_cents)}</dd>
          <dt>Ordering fee (provider)</dt><dd className="num neg">−{money(order.ordering_fee_cents)}</dd>
          <dt>Original profit</dt><dd className={`num ${profitTone(order.original_profit_cents)}`}>{money(order.original_profit_cents)}</dd>
          <dt>Effective profit</dt>
          <dd className={`num ${profitTone(order.effective_profit_cents)}`}>
            {money(order.effective_profit_cents)}{" "}
            {order.excluded && (
              <span className="muted small">
                ({order.display_status.toLowerCase()}{order.ordering_fee_cents > 0 && " · ordering fee still charged"})
              </span>
            )}
          </dd>
          <dt>Margin / ROI</dt><dd className="num">{percent(order.margin)} / {percent(order.roi)}</dd>
        </dl>
        {order.revenue_estimated && (
          <p className="muted small">Estimated from the order until eBay posts the payout; ad fees are added then.</p>
        )}
      </section>

      <section>
        <h3>Buyer / shipping</h3>
        <dl className="kv">
          <dt>Name</dt><dd>{order.ship_name ?? "—"}</dd>
          <dt>Address</dt><dd>{[order.address1, order.address2].filter(Boolean).join(", ") || "—"}</dd>
          <dt>City</dt><dd>{order.city ?? "—"}</dd>
          <dt>State</dt><dd>{order.state ?? "—"}</dd>
          <dt>ZIP</dt><dd>{order.postal_code ?? "—"}</dd>
          <dt>Country</dt><dd>{order.country ?? "—"}</dd>
        </dl>
      </section>

      <section className="wide">
        <h3>Amazon cost</h3>
        {order.matched_email ? (
          <div>
            Linked email: <b>{order.matched_email.subject}</b> · {dateTime(order.matched_email.received_at)} · Grand Total{" "}
            {money(order.matched_email.grand_total_cents)}{" "}
            <button className="btn small danger" disabled={busy} onClick={() => act("/match", { method: "DELETE" })}>Unlink</button>
          </div>
        ) : (
          <div className="muted">No Amazon email linked.</div>
        )}
        {order.manual_cost_cents !== null && (
          <div style={{ marginTop: 6 }}>
            Manual cost {money(order.manual_cost_cents)}{" "}
            <button className="btn small" disabled={busy} onClick={() => saveCost(null)}>Clear</button>
          </div>
        )}
        {order.candidates.length > 0 && !order.matched_email && (
          <table className="data" style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Candidate email</th><th>Received</th><th className="num">Total</th><th className="num">Score</th><th>Why</th><th /></tr>
            </thead>
            <tbody>
              {order.candidates.map((c) => (
                <tr key={c.gmail_message_id}>
                  <td className="truncate" style={{ maxWidth: 260 }}>{c.email?.subject ?? c.gmail_message_id}</td>
                  <td>{c.email ? dateTime(c.email.received_at) : "—"} <span className="muted small">({signedMinutes(c.minutes_after)})</span></td>
                  <td className="num">{money(c.email?.grand_total_cents)}</td>
                  <td className="num">{c.score.toFixed(0)}</td>
                  <td className="small muted">
                    {Object.entries(c.signals).map(([name, s]) => `${name} ${s.points >= 0 ? "+" : ""}${s.points}`).join(" · ")}
                  </td>
                  <td>
                    {c.email?.attached_to ? <span className="muted small">used by {c.email.attached_to}</span> :
                      <button className="btn small" disabled={busy} onClick={() => link(c.gmail_message_id)}>Link</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="row-actions">
          <input className="input" placeholder="Amazon cost, e.g. 23.45" value={cost} onChange={(e) => setCost(e.target.value)}
            style={{ width: 190 }} />
          <button className="btn" disabled={busy || !cost} onClick={() => saveCost(cost)}>Save cost</button>
          <button className="btn" disabled={busy || locked} onClick={() => act("/recheck", { method: "POST" })}
            title={locked ? "Already matched or set manually: unlink or clear the manual cost first" : "Score this order against Amazon emails again"}>
            Recheck Amazon match
          </button>
          <button className="btn" onClick={loadEmails}>Find email manually…</button>
          <span className="spacer" />
          <label className="muted small">Return status</label>
          <select className="input" value={order.manual_return_status ?? ""} disabled={busy}
            onChange={(e) => act("/return-status", { method: "PUT", body: JSON.stringify({ status: e.target.value || null }) })}>
            <option value="">From eBay ({order.ebay_return_status ?? "NONE"})</option>
            {RETURN_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        {emails && (
          <table className="data" style={{ marginTop: 10 }}>
            <thead><tr><th>Amazon email (1 day before – 3 days after)</th><th>Received</th><th>Ship to</th><th className="num">Total</th><th>Parse</th><th /></tr></thead>
            <tbody>
              {emails.length === 0 && <tr><td colSpan={6} className="muted">No Amazon confirmations in that window.</td></tr>}
              {emails.map((e) => (
                <tr key={e.gmail_message_id}>
                  <td className="truncate" style={{ maxWidth: 280 }}>{e.subject}</td>
                  <td>{dateTime(e.received_at)}</td>
                  <td>{[e.first_name, e.city, e.state].filter(Boolean).join(", ") || "—"}</td>
                  <td className="num">{money(e.grand_total_cents)}</td>
                  <td><Badge value={e.parse_status} tone={e.parse_status === "parsed" ? "green" : "amber"} /></td>
                  <td>
                    {e.attached_to === order.order_id ? <span className="muted small">linked</span> : (
                      <button className="btn small" disabled={busy} onClick={() => link(e.gmail_message_id, !!e.attached_to)}>
                        {e.attached_to ? `Move from ${e.attached_to}` : "Link"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
