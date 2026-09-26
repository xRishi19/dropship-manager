"use client";

import { Fragment, useState } from "react";
import Badge from "@/components/Badge";
import { dateTime, money, OrderSummary } from "@/lib/api";
import OrderDetailPanel from "./OrderDetailPanel";

function ProfitCell({ order }: { order: OrderSummary }) {
  if (order.excluded) return <span className="muted">{money(0)}</span>;
  if (order.effective_profit_cents === null) {
    return <span className="neg small">{order.cost_known ? "—" : "Cost unknown"}</span>;
  }
  return <span className={order.effective_profit_cents >= 0 ? "pos" : "neg"}>{money(order.effective_profit_cents)}</span>;
}

export default function OrdersTable({ orders, onChanged }: { orders: OrderSummary[]; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!orders.length) return <div className="empty">No orders in this view.</div>;
  return (
    <table className="data">
      <thead>
        <tr>
          <th>Product</th>
          <th className="num">Profit</th>
          <th>Status</th>
          <th>Date</th>
          <th>Return Status</th>
        </tr>
      </thead>
      <tbody>
        {orders.map((order) => (
          <Fragment key={order.order_id}>
            <tr className={`row ${open === order.order_id ? "open" : ""}`}
              onClick={() => setOpen(open === order.order_id ? null : order.order_id)}>
              <td>
                <div className="truncate">{order.product_title}</div>
                <div className="badges" style={{ marginTop: 4 }}>
                  {order.quantity > 1 && <Badge value={`×${order.quantity}`} tone="gray" />}
                  {order.match_status === "NEEDS_REVIEW" && <Badge value="NEEDS_REVIEW" />}
                  {!order.cost_known && !order.excluded && order.match_status !== "NEEDS_REVIEW" && (
                    <Badge value="unmatched" tone="amber" label="Amazon cost unknown" />
                  )}
                  {order.revenue_estimated && !order.excluded && <Badge value="est" tone="gray" label="revenue estimated" />}
                </div>
              </td>
              <td className="num"><ProfitCell order={order} /></td>
              <td><Badge value={order.display_status} /></td>
              <td className="muted">{dateTime(order.created_at)}</td>
              <td>{order.return_status !== "NONE" ? <Badge value={order.return_status} /> : <span className="faint">—</span>}</td>
            </tr>
            {open === order.order_id && (
              <tr>
                <td colSpan={5} style={{ background: "var(--bg)" }}>
                  <OrderDetailPanel orderId={order.order_id} onChanged={onChanged} />
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}
