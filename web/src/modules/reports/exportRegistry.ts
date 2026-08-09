/**
 * Per-tab witnessed-exports registry (module 8 — honesty side).
 *
 * WHY THIS EXISTS: the contract exposes NO export list endpoint —
 * `ExportOut` is reachable only as the response of `POST /exports`
 * (this tab's own request) or `GET /exports/{id}` (requester-only, and
 * the id must already be known). The screen's follow-up affordances
 * (refresh status, download CSV/PDF) need the export id, and the only
 * place this client can honestly obtain it is a response THIS TAB
 * witnessed. The registry records those records verbatim.
 *
 * Scope and honesty (the guarantors sessionRegistry precedent):
 * the registry is per-tab and in-memory — it cannot see exports
 * requested by another operator, in another tab, or before this page
 * load; those artifacts remain reachable through the backend audit
 * trail until a list endpoint ships (recorded as a contract gap in the
 * MR). Nothing here is a source of truth: every field shown from the
 * registry is a SERVER response rendered verbatim, and every refresh
 * replaces the record with the server's current one.
 */
import { useSyncExternalStore } from "react";
import { registerSessionScopedStore } from "@/modules/auth/sessionScopedStores";
import type { ExportOut } from "./schemas";

let entries: readonly ExportOut[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Record (or update in place) an export exactly as the server
 * returned it. Newest activity first; one row per export id. */
export function recordWitnessedExport(record: ExportOut): void {
  entries = [record, ...entries.filter((entry) => entry.id !== record.id)];
  emit();
}

/** Session-teardown hygiene (wired structurally per W58-2):
 * registered as a session-scoped store below, so BOTH teardown paths —
 * the query-path 401 dual-cache teardown (app providers) and explicit
 * sign-out (auth logout) — clear it. Witnessed export records (ids,
 * scopes, download paths) never survive an in-tab operator switch.
 * Also test hygiene. */
export function clearWitnessedExports(): void {
  entries = [];
  emit();
}

// Teardown wiring by construction (W58-2): registration at module scope
// means the registry cannot exist without dying on session teardown.
registerSessionScopedStore(clearWitnessedExports);

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): readonly ExportOut[] {
  return entries;
}

/** The exports this tab witnessed, newest activity first. */
export function useWitnessedExports(): readonly ExportOut[] {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
