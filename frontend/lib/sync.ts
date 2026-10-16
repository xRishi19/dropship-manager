"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { SyncStatus } from "./api";

// Fired on window by SyncIndicator whenever a sync step or cycle finishes (automatic or Sync Now).
const SYNCED = "dropship:synced";

// Changes whenever accounting results land. Listings and notification delivery don't change order data.
/**
 * Build a stable signature of accounting sync results so pages refresh when new data lands. Listings and Discord delivery are excluded because they do not change accounting data.
 *
 * @param status - Latest sync loop status and per-step success timestamps.
 * @returns Deterministic string containing the cycle finish time and sorted accounting step timestamps.
 */
export function syncSignature(status: SyncStatus): string {
  const steps = Object.entries(status.steps)
    .filter(([name]) => name !== "listings" && name !== "discord")
    .map(([name, step]) => `${name}=${step.last_success_at ?? ""}`)
    .sort();
  return [status.loop.last_finished ?? "", ...steps].join("|");
}

/**
 * Notify browser listeners that new sync results are available by dispatching the shared window event.
 *
 * @remarks Takes no arguments.
 * @returns Nothing; dispatches the dropship:synced event synchronously.
 */
export function announceSync() {
  window.dispatchEvent(new Event(SYNCED));
}

/**
 * Subscribe a component to completed-sync events while keeping the latest refresh callback without resubscribing. Removes the listener when the component unmounts.
 *
 * @param refresh - Callback invoked for each shared sync event; its return value is ignored.
 * @returns Nothing; registers React effects and a browser event listener.
 */
export function useSyncRefresh(refresh: () => void) {
  const latest = useRef(refresh);
  useLayoutEffect(() => {
    latest.current = refresh;
  });
  useEffect(() => {
    /**
     * Invoke the latest refresh callback when the shared sync event arrives.
     *
     * @remarks Takes no arguments.
     * @returns The return value of the refresh callback; the event system ignores it.
     */
    const handler = () => latest.current();
    window.addEventListener(SYNCED, handler);
    return () => window.removeEventListener(SYNCED, handler);
  }, []);
}
