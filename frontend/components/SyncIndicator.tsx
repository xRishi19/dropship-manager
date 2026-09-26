"use client";

import { useCallback, useEffect, useState } from "react";
import { api, SyncStatus, timeAgo } from "@/lib/api";

const STEP_LABELS: Record<string, string> = {
  orders: "eBay orders",
  orders_backfill: "Order history (since Jan 1)",
  finances: "eBay finances",
  finances_backfill: "Finance history (since Jan 1)",
  returns: "eBay returns",
  categories: "Categories",
  gmail: "Gmail",
  reconcile: "Amazon matching",
  listings: "Listings (daily, a few minutes)",
};

export default function SyncIndicator() {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [requested, setRequested] = useState<string | null>(null); // last_finished when Sync Now was clicked

  const load = useCallback(() => {
    api<SyncStatus>("/sync/status")
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const syncNow = () => {
    setRequested(status?.loop.last_finished ?? "");
    api<SyncStatus>("/sync/now", { method: "POST" }).then(setStatus).catch((e: Error) => setError(e.message));
    setTimeout(load, 1500);
  };
  // Show "Syncing…" from the click until a newer cycle has finished.
  const waiting = requested !== null && (status?.loop.last_finished ?? "") === requested;
  useEffect(() => {
    if (requested !== null && !waiting) setRequested(null);
  }, [requested, waiting]);
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [waiting, load]);

  const failing = status ? Object.entries(status.steps).filter(([name, s]) => STEP_LABELS[name] && s.last_error) : [];
  const dot = error ? "warn" : status?.loop.running || waiting ? "busy" : failing.length ? "warn" : status?.loop.last_finished ? "ok" : "";
  const label = error
    ? "Backend unreachable"
    : status?.loop.running || waiting
      ? "Syncing…"
      : `Synced ${timeAgo(status?.loop.last_finished ?? status?.steps.cycle?.last_success_at)}`;

  return (
    <div className="sync" style={{ position: "relative" }}>
      <span className={`dot ${dot}`} />
      <button className="btn small" onClick={() => setOpen(!open)} title="Sync details">
        {label}
        {failing.length ? ` · ${failing.length} issue${failing.length > 1 ? "s" : ""}` : ""}
      </button>
      <button className="btn small" onClick={syncNow} disabled={!!error || status?.loop.running || waiting}>
        Sync Now
      </button>
      {open && status && (
        <div className="card" style={{ position: "absolute", right: 0, top: 34, width: 420, zIndex: 20 }}>
          <h2>Sync steps (every {status.loop.interval_seconds}s)</h2>
          <table className="data">
            <tbody>
              {Object.entries(STEP_LABELS).map(([name, title]) => {
                const step = status.steps[name];
                const skipped = step?.details && "skipped" in step.details ? String(step.details.skipped) : null;
                const complete = step?.details && step.details.backfill_complete === true;
                const active = status.loop.current_step === name;
                return (
                  <tr key={name}>
                    <td>{title}</td>
                    <td className="small">
                      {active ? (
                        <span style={{ color: "var(--blue)" }}>Running…</span>
                      ) : step?.last_error ? (
                        <span className="neg">{step.last_error}</span>
                      ) : skipped && skipped !== "not due" ? (
                        <span className="muted">{skipped}</span>
                      ) : !step?.last_success_at ? (
                        <span className="faint">{status.loop.running ? "Waiting for this cycle" : "Not run yet"}</span>
                      ) : (
                        <span className="muted">
                          OK {timeAgo(step.last_success_at)}
                          {name.endsWith("_backfill") && !complete && step.cursor && step.cursor !== "complete"
                            ? ` · through ${String(step.cursor).slice(0, 10)}` : ""}
                          {complete || step.cursor === "complete" ? " · complete" : ""}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!status.gmail_connected && (
            <p className="notice small" style={{ marginBottom: 0 }}>
              Gmail isn&apos;t connected, so Amazon costs can&apos;t be matched automatically. See docs/setup.md.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
