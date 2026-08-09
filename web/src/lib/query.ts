/**
 * TanStack Query conventions (one copy, reuse-first).
 *
 * KEY SHAPES (every screen follows these — greppable, collision-free):
 *   [entity, "list", filters?] — keyset lists (filters object included
 *                                  when the screen filters server-side)
 *   [entity, "detail", id] — single records backing drawers/edits
 *   ["access", "roles"] — reference data (roles)
 *   ["access", "permissions", roleId]
 *   ["me", "permissions"] — the signed-in grant matrix
 *   ["dashboard", "summary"] — composite read models
 *
 * STALE TIMES per entity class (why each value):
 *   - reference: roles/products change rarely; refetching them on every
 *     drawer mount is waste. 5 min.
 *   - composite: dashboard aggregates are server-computed snapshots with
 *     an as_of stamp rendered next to them. 60 s.
 *   - list: keyset pages should stay fresh across tab switches but not
 *     refetch on every render. 30 s.
 *   - record: 0 — NEVER serve a stale record into an optimistic-locked
 *     edit; the version the form loads must be the freshest available
 *     (the 409 flow covers the rest).
 */
export const STALE_TIME = {
  reference: 5 * 60_000,
  composite: 60_000,
  list: 30_000,
  record: 0,
} as const;
