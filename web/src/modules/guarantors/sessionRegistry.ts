/**
 * Per-tab witnessed-guarantee registry (module 5 — honesty side).
 *
 * WHY THIS EXISTS: the P9/ contract exposes NO guarantee
 * list/read endpoint — GuaranteeOut exists ONLY as the response of the
 * four writes (pledge, consent, release, substitute). The screen's
 * follow-up actions (consent a pledge, release, substitute) need the
 * guarantee id + version, and the only place this client can honestly
 * obtain them is a write response THIS TAB witnessed. The registry
 * records those records verbatim (id, parties, decimal-string amount,
 * status, version) so the operator can carry a pledge through consent
 * and release within their session.
 *
 * Scope and honesty (the makerRegistry precedent): the registry is
 * per-tab and in-memory — it cannot see guarantees pledged by another
 * operator, in another tab, or before this page load; those records are
 * reachable only through the backend audit trail until a list endpoint
 * ships (recorded as follow-up scope in the MR). The ENFORCED
 * invariants are server-side regardless (least disclosure): capacity under the
 * guarantor's deposit-account row lock, the status machine
 * (pledged -> active -> released), optimistic-lock versions and the
 * cover rule. Nothing here is a source of truth — every figure shown
 * from the registry is a SERVER response rendered verbatim.
 */
import { useSyncExternalStore } from "react";
import { registerSessionScopedStore } from "@/modules/auth/sessionScopedStores";
import type { Guarantee } from "./schemas";

export interface WitnessedGuarantee {
  /** The server's record, verbatim (the last write response this tab saw). */
  record: Guarantee;
  /**
   * Set when a versioned act on this record returned 409: the record
   * moved OUTSIDE this tab and — with no read endpoint to refresh it —
   * its affordances are structurally withdrawn (reload-never-replay).
   */
  conflicted: boolean;
}

let entries: readonly WitnessedGuarantee[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

/** Record (or update in place) a guarantee exactly as the server
 * returned it. Newest activity first; one row per guarantee id; a fresh
 * server response clears any prior conflict marking. */
export function recordWitnessedGuarantee(guarantee: Guarantee): void {
  entries = [
    { record: guarantee, conflicted: false },
    ...entries.filter((entry) => entry.record.id !== guarantee.id),
  ];
  emit();
}

/** Withdraw a record's affordances after a 409 (the tab's copy is stale
 * and cannot be re-read — acting again requires a fresh server response). */
export function markWitnessedConflict(guaranteeId: string): void {
  entries = entries.map((entry) =>
    entry.record.id === guaranteeId ? { ...entry, conflicted: true } : entry,
  );
  emit();
}

/** Session-teardown hygiene (wired structurally per W58-2):
 * registered as a session-scoped store below, so BOTH teardown paths —
 * the query-path 401 dual-cache teardown (app providers) and explicit
 * sign-out (auth logout) — clear it. Witnessed financial records and
 * the affordances armed on them never survive an in-tab operator
 * switch. Also test hygiene. */
export function clearWitnessedGuarantees(): void {
  entries = [];
  emit();
}

// Teardown wiring by construction (W58-2): registration at module scope
// means the registry cannot exist without dying on session teardown.
registerSessionScopedStore(clearWitnessedGuarantees);

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): readonly WitnessedGuarantee[] {
  return entries;
}

/** The guarantees this tab witnessed, newest activity first. */
export function useWitnessedGuarantees(): readonly WitnessedGuarantee[] {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
