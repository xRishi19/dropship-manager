"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Badge from "@/components/Badge";
import { api, Expense, ExpenseInput, ExpensesResponse, money, OrderingFees } from "@/lib/api";
import { useSyncRefresh } from "@/lib/sync";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Parse "YYYY-MM-DD" as plain numbers so no timezone can shift the day.
function parts(iso: string): [number, number, number] {
  const [y, m, d] = iso.split("-").map(Number);
  return [y, m, d];
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = parts(iso);
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}

function ordinal(n: number): string {
  const rem100 = n % 100;
  if (rem100 >= 11 && rem100 <= 13) return `${n}th`;
  return `${n}${{ 1: "st", 2: "nd", 3: "rd" }[n % 10] ?? "th"}`;
}

function when(e: Expense): string {
  if (!e.recurring) return fmtDate(e.start_date);
  const [y, m, d] = parts(e.start_date);
  const base = `Monthly on the ${ordinal(d)}, since ${MONTHS[m - 1]} ${y}`;
  return e.end_date ? `${base} · ends ${fmtDate(e.end_date)}` : base;
}

function isEnded(e: Expense): boolean {
  return e.recurring && e.end_date !== null && (e.end_date < today() || !e.next_occurrence);
}

/** Read-only: the provider fee is charged per eBay order (cancelled included) and is already in each order's profit. */
function OrderingFeesCard({ fees }: { fees: OrderingFees | undefined }) {
  const line = (count: number, cents: number) => `${count} order${count === 1 ? "" : "s"} · ${money(cents)}`;
  return (
    <div className="card stat">
      <div className="label">Ordering provider fees</div>
      <div className="value">
        {fees ? money(fees.per_order_cents) : "—"} <span className="muted small" style={{ fontWeight: 400 }}>per order</span>
      </div>
      {fees && (
        <dl className="kv small" style={{ marginTop: 6 }}>
          <dt>This month</dt><dd className="num">{line(fees.this_month_count, fees.this_month_cents)}</dd>
          <dt>Year to date</dt><dd className="num">{line(fees.ytd_count, fees.ytd_cents)}</dd>
          <dt>All time</dt><dd className="num">{line(fees.all_count, fees.all_cents)}</dd>
        </dl>
      )}
      <div className="sub">Automatic · already included in order profit (not subtracted again)</div>
    </div>
  );
}

const EMPTY_FORM = (): ExpenseInput => ({ name: "", amount: "", date: today(), recurring: false, end_date: null });

export default function ExpensesPage() {
  const [data, setData] = useState<ExpensesResponse | null>(null);
  const [form, setForm] = useState<ExpenseInput>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    () => api<ExpensesResponse>("/expenses").then(setData).catch((e: Error) => setError(e.message)),
    [],
  );
  useEffect(() => { load(); }, [load]);
  useSyncRefresh(load); // Ordering-fee counts follow new orders.

  const mutate = async (path: string, init: RequestInit) => {
    setBusy(true);
    setError(null);
    try {
      await api<unknown>(path, init);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
      load();
    }
  };

  const reset = () => { setForm(EMPTY_FORM()); setEditingId(null); };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const body: ExpenseInput = {
      name: form.name.trim(),
      amount: form.amount.trim().replace(/^\$/, ""),
      date: form.date,
      recurring: form.recurring,
      end_date: form.recurring && form.end_date ? form.end_date : null,
    };
    const ok = await mutate(editingId === null ? "/expenses" : `/expenses/${editingId}`, {
      method: editingId === null ? "POST" : "PUT",
      body: JSON.stringify(body),
    });
    if (ok) reset();
  };

  const edit = (e: Expense) => {
    setEditingId(e.id);
    setConfirmDelete(null);
    setError(null);
    setForm({
      name: e.name,
      amount: (e.amount_cents / 100).toFixed(2),
      date: e.start_date,
      recurring: e.recurring,
      end_date: e.end_date,
    });
  };

  const stop = (e: Expense) => {
    const now = today();
    if (e.start_date > now) {
      setError(`"${e.name}" hasn't started yet; delete it instead of stopping it.`);
      return;
    }
    const body: ExpenseInput = {
      name: e.name, amount: (e.amount_cents / 100).toFixed(2), date: e.start_date, recurring: true, end_date: now,
    };
    mutate(`/expenses/${e.id}`, { method: "PUT", body: JSON.stringify(body) });
  };

  const remove = async (id: number) => {
    setConfirmDelete(null);
    const ok = await mutate(`/expenses/${id}`, { method: "DELETE" });
    if (ok && editingId === id) reset();
  };

  return (
    <>
      <div className="page-head"><h1>Expenses</h1></div>

      <div className="cards">
        <div className="card stat">
          <div className="label">This month</div>
          <div className="value">{data ? money(data.this_month_cents) : "—"}</div>
          <div className="sub">All expenses dated this calendar month</div>
        </div>
        <div className="card stat">
          <div className="label">Monthly recurring</div>
          <div className="value">{data ? money(data.monthly_recurring_cents) : "—"}</div>
          <div className="sub">Active subscriptions per month</div>
        </div>
        <OrderingFeesCard fees={data?.ordering_fees} />
      </div>

      <form className="card expense-form" onSubmit={submit}>
        <h2>{editingId === null ? "Add expense" : "Edit expense"}</h2>
        <div className="expense-fields">
          <label>
            <span>Name</span>
            <input className="input" value={form.name} placeholder="e.g. Shopify plan" required
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </label>
          <label>
            <span>Amount</span>
            <input className="input" inputMode="decimal" value={form.amount} placeholder="12.99" required
              onChange={(e) => setForm({ ...form, amount: e.target.value })} />
          </label>
          <label>
            <span>{form.recurring ? "Starts on" : "Date"}</span>
            <input className="input" type="date" value={form.date} required
              onChange={(e) => setForm({ ...form, date: e.target.value })} />
          </label>
          <label className="expense-check">
            <input type="checkbox" checked={form.recurring}
              onChange={(e) => setForm({ ...form, recurring: e.target.checked })} />
            <span>Repeats monthly</span>
          </label>
          {form.recurring && (
            <label>
              <span>Ends on <span className="faint">(optional)</span></span>
              <input className="input" type="date" value={form.end_date ?? ""} min={form.date || undefined}
                onChange={(e) => setForm({ ...form, end_date: e.target.value || null })} />
            </label>
          )}
        </div>
        <div className="row-actions">
          <button className="btn primary" type="submit" disabled={busy}>
            {editingId === null ? "Add expense" : "Save changes"}
          </button>
          {(editingId !== null || form.name || form.amount) && (
            <button className="btn" type="button" onClick={reset} disabled={busy}>Cancel</button>
          )}
        </div>
      </form>

      {error && <p className="error">{error}</p>}

      <div className="card expense-list">
        {!data ? (
          <div className="empty">Loading…</div>
        ) : data.expenses.length === 0 ? (
          <div className="empty">No expenses yet.</div>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Name</th>
                <th className="num">Amount</th>
                <th>When</th>
                <th>Status</th>
                <th>Next charge</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.expenses.map((e) => {
                const ended = isEnded(e);
                return (
                  <tr key={e.id} className={editingId === e.id ? "open" : ""}>
                    <td>{e.name}</td>
                    <td className="num">{money(e.amount_cents)}</td>
                    <td>{when(e)}</td>
                    <td>
                      {!e.recurring ? <Badge value="one-time" tone="gray" label="One-time" />
                        : ended ? <Badge value="ended" tone="gray" label="Ended" />
                        : <Badge value="active" tone="green" label="Active monthly" />}
                    </td>
                    <td>{fmtDate(e.next_occurrence)}</td>
                    <td>
                      <div className="expense-actions">
                        <button className="btn small" disabled={busy} onClick={() => edit(e)}>Edit</button>
                        {e.recurring && !ended && (
                          <button className="btn small" disabled={busy} onClick={() => stop(e)}>Stop</button>
                        )}
                        {confirmDelete === e.id ? (
                          <>
                            <span className="small muted">Confirm delete?</span>
                            <button className="btn small danger" disabled={busy} onClick={() => remove(e.id)}>Delete</button>
                            <button className="btn small" onClick={() => setConfirmDelete(null)}>Keep</button>
                          </>
                        ) : (
                          <button className="btn small danger" disabled={busy} onClick={() => setConfirmDelete(e.id)}>Delete</button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
