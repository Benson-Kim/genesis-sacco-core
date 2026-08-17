/**
 * Shared per-tab session-scoped registry primitive (issue #30 finding
 * S3 — the deferred consolidation assigned to the first issue-#31
 * batch landing after the corrections and dividends consoles).
 *
 * WHY ONE COPY (gate 1.1): the "per-tab witnessed map, torn down with
 * the session" idea existed as near-identical per-module copies (the
 * applications, exits and corrections maker registries and voted
 * registries). Each copy re-implemented the same three obligations:
 *
 *  1. module-scope registration with the session-scoped store
 *     registry, so BOTH teardown paths — the query-path 401 dual-cache
 *     teardown (`app/providers.tsx`) and explicit sign-out
 *     (`modules/auth/api.ts`) — clear it (W58-2, the !60 F2 class);
 *  2. plain synchronous reads for the SoD fallback witness consumed by
 *     MakerCheckerPanel-bearing screens;
 *  3. a `useSyncExternalStore`-backed reactive read so a spent
 *     affordance stays spent across component remounts (W58-6).
 *
 * This factory is that single copy. The per-module registry files
 * remain as thin vocabulary-preserving wrappers: their exported names,
 * sentinels and docblocked honesty notes are the module's API and stay
 * byte-compatible, so every existing consumer and test keeps working
 * unmodified (EXTEND NEVER WEAKEN).
 *
 * Behaviour contract (identical to the copies this replaces):
 * - per-tab and in-memory — it cannot see acts by another operator or
 *   in another tab; the ENFORCED invariants remain server-side
 *   regardless (gate 1.6);
 * - `clear` is registered at construction: a registry cannot exist
 *   without dying on session teardown (teardown by construction);
 * - `get` NEVER invents a value — an unwitnessed key is `undefined`;
 *   UNKNOWN-maker sentinels are wrapper vocabulary, not registry
 *   state;
 * - the reactive read's server-render snapshot is the empty state.
 */
import { useSyncExternalStore } from "react";
import { registerSessionScopedStore } from "./sessionScopedStores";

export interface SessionScopedRegistry<K, V> {
  /** Record (or overwrite) what THIS TAB witnessed for `key`. */
  set(key: K, value: V): void;
  /** The witnessed value, or `undefined` when this tab saw nothing. */
  get(key: K): V | undefined;
  /** Whether THIS TAB witnessed `key` at all. */
  has(key: K): boolean;
  /** Empty the registry (session teardown and test hygiene). */
  clear(): void;
  /**
   * Reactive `has` for components (re-renders on set and on clear).
   * The server-render snapshot is always the empty state: nothing is
   * ever witnessed outside a live tab.
   */
  useHas(key: K): boolean;
}

/**
 * Build a per-tab witnessed registry wired into session teardown by
 * construction. Call ONCE at module scope of the owning wrapper —
 * construction itself is the W58-2 teardown wiring.
 */
export function createSessionScopedRegistry<K, V>(): SessionScopedRegistry<K, V> {
  const entries = new Map<K, V>();
  const listeners = new Set<() => void>();

  function emit(): void {
    for (const listener of listeners) listener();
  }

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }

  function set(key: K, value: V): void {
    entries.set(key, value);
    emit();
  }

  function get(key: K): V | undefined {
    return entries.get(key);
  }

  function has(key: K): boolean {
    return entries.has(key);
  }

  function clear(): void {
    entries.clear();
    emit();
  }

  function useHas(key: K): boolean {
    return useSyncExternalStore(
      subscribe,
      () => entries.has(key),
      () => false,
    );
  }

  // Teardown wiring by construction (W58-2): registering here means a
  // registry cannot exist without dying on BOTH session-death paths.
  registerSessionScopedStore(clear);

  return { set, get, has, clear, useHas };
}
