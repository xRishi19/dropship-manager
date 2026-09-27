"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import { SyncStatus } from "./api";

// Fired on window by SyncIndicator whenever a sync step or cycle finishes (automatic or Sync Now).
const SYNCED = "dropship:synced";

// Changes whenever a cycle, or any step inside it, completes. Listings are left out: they don't touch orders.
export function syncSignature(status: SyncStatus): string {
  const steps = Object.entries(status.steps)
    .filter(([name]) => name !== "listings")
    .map(([name, step]) => `${name}=${step.last_success_at ?? ""}`)
    .sort();
  return [status.loop.last_finished ?? "", ...steps].join("|");
}

export function announceSync() {
  window.dispatchEvent(new Event(SYNCED));
}

/** Calls `refresh` after every sync, always using the latest `refresh` passed in. */
export function useSyncRefresh(refresh: () => void) {
  const latest = useRef(refresh);
  useLayoutEffect(() => {
    latest.current = refresh;
  });
  useEffect(() => {
    const handler = () => latest.current();
    window.addEventListener(SYNCED, handler);
    return () => window.removeEventListener(SYNCED, handler);
  }, []);
}
