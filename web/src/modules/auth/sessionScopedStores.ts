/**
 * Session-scoped store registry (W58-2 — the F2 class, closed structurally).
 *
 * WHY THIS EXISTS: several modules keep small per-tab in-memory
 * registries of state THIS TAB witnessed (the applications
 * makerRegistry, the guarantors witnessed-guarantee registry, the
 * committee voted registry). Any such store MUST die with the session:
 * if it survives an in-tab operator switch, the next operator inherits
 * the previous operator's witnessed attributions and armed affordances
 * — an attribution and least-privilege failure. The first two
 * instances of this class were fixed by hand-wiring each clear function
 * into both teardown paths; this registry makes the wiring structural:
 * a per-tab store registers its clear function ONCE, at module scope,
 * and is torn down by construction.
 *
 * Teardown callers (BOTH session-death paths — never only one):
 *  - query-path 401 dual-cache teardown (`app/providers.tsx`);
 *  - explicit sign-out `finally` (`modules/auth/api.ts`).
 */

type ClearFn = () => void;

const clearFns = new Set<ClearFn>();

/**
 * Register a per-tab store's clear function. Call ONCE at module scope
 * of the store — registration itself is the teardown wiring.
 */
export function registerSessionScopedStore(clear: ClearFn): void {
  clearFns.add(clear);
}

/**
 * Clear EVERY registered per-tab store. Invoked from both session
 * teardown paths; nothing witnessed under one operator's identity
 * survives into the next session.
 */
export function clearSessionScopedStores(): void {
  for (const clear of clearFns) clear();
}
