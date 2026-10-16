"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, SyncStatus, timeAgo } from "@/lib/api";
import { announceSync, syncSignature } from "@/lib/sync";

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

/**
 * Render sync status, per-step controls, full synchronization, and the webhook test action. Poll status regularly and faster while busy, then notify pages when accounting results change.
 *
 * @remarks Takes no arguments.
 * @returns React element containing the status buttons and optional sync-detail menu.
 */
export default function SyncIndicator() {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [testingWebhook, setTestingWebhook] = useState(false);
  const [webhookResult, setWebhookResult] = useState<{ ok: boolean; message: string } | null>(null);
  const [open, setOpen] = useState(false);
  const [requested, setRequested] = useState<string | null>(null); // last_finished when Sync Now was clicked
  const [requestedStep, setRequestedStep] = useState<string | null>(null);
  const signature = useRef<string | null>(null);

  /**
   * Apply a successful status response and announce changed accounting results after the initial response.
   *
   * @param s - Latest backend sync status, including loop and step timestamps.
   * @returns Nothing; updates status/error state, the stored signature, and possibly dispatches a refresh event.
   */
  const apply = useCallback((s: SyncStatus) => {
    setStatus(s);
    setError(null);
    // Tell the open pages to reload once new sync results land (not on the first status after page load).
    const next = syncSignature(s);
    if (signature.current !== null && signature.current !== next) announceSync();
    signature.current = next;
  }, []);

  /**
   * Fetch current backend synchronization status and display connection errors if polling fails.
   *
   * @remarks Takes no arguments.
   * @returns Nothing; starts a request whose result updates status or the connection error.
   */
  const load = useCallback(() => {
    api<SyncStatus>("/sync/status").then(apply).catch((e: Error) => setError(e.message));
  }, [apply]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  /**
   * Send the backend’s sample Discord notification and display its confirmed result or a request error. Track test progress separately from accounting sync state.
   *
   * @remarks Takes no arguments.
   * @returns Promise resolving with no value after the test result is displayed and the busy flag clears.
   */
  const testWebhook = async () => {
    setTestingWebhook(true);
    setWebhookResult(null);
    try {
      setWebhookResult(await api<{ ok: boolean; message: string }>("/sync/webhook/test", { method: "POST" }));
    } catch (e) {
      setWebhookResult({ ok: false, message: e instanceof Error ? e.message : "Webhook test failed." });
    } finally {
      setTestingWebhook(false);
    }
  };

  /**
   * Request a full sync or one named step, displaying immediate pending state and polling for completion. Clear pending state and show an error if the request fails.
   *
   * @param name - Optional backend sync-step name; omitted runs the full cycle.
   * @returns Nothing; starts the sync request and schedules a follow-up status refresh.
   */
  const syncNow = (name?: string) => {
    setRequestError(null);
    setRequested(status?.loop.last_finished ?? "");
    setRequestedStep(name ?? null);
    api<SyncStatus>(name ? `/sync/steps/${name}` : "/sync/now", { method: "POST" })
      .then(apply)
      .catch((e: Error) => {
        setRequested(null);
        setRequestedStep(null);
        setRequestError(e.message);
        load();
      });
    setTimeout(load, 1500);
  };
  // Show "Syncing…" from the click until a newer cycle has finished.
  const waiting = requested !== null && (status?.loop.last_finished ?? "") === requested;
  useEffect(() => {
    if (requested !== null && !waiting) {
      setRequested(null);
      setRequestedStep(null);
    }
  }, [requested, waiting]);
  // Poll faster while a cycle runs, so each step's results show up soon after it finishes.
  const busy = waiting || !!status?.loop.running;
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [busy, load]);

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
      <button className="btn small" onClick={() => syncNow()} disabled={!!error || status?.loop.running || waiting}>
        Sync Now
      </button>
      {open && status && (
        <div className="card" style={{ position: "absolute", right: 0, top: 34, width: 520, maxWidth: "calc(100vw - 32px)", zIndex: 20 }}>
          <h2>Sync steps (every {status.loop.interval_seconds}s)</h2>
          <table className="data">
            <tbody>
              {Object.entries(STEP_LABELS).map(([name, title]) => {
                const step = status.steps[name];
                const skipped = step?.details && "skipped" in step.details ? String(step.details.skipped) : null;
                const complete = step?.details && step.details.backfill_complete === true;
                const active = status.loop.current_step === name || (waiting && requestedStep === name);
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
                    <td>
                      <button className="btn small" onClick={() => syncNow(name)} disabled={busy}
                        aria-label={`Sync ${title}`}>
                        {active ? "Syncing…" : "Sync"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <button className="btn small" onClick={testWebhook} disabled={testingWebhook}>
            {testingWebhook ? "Sending test…" : "Test Webhook"}
          </button>
          {webhookResult && (
            <p className={webhookResult.ok ? "small muted" : "notice small"} role="status">
              {webhookResult.message}
            </p>
          )}
          {(error || requestError) && <p className="notice small">{error || requestError}</p>}
          {status.steps.discord?.last_error && (
            <p className="notice small">Sale notifications: {status.steps.discord.last_error}</p>
          )}
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
