/**
 * Inactivity sign-out — code-owned timing vocabulary (#35).
 *
 * SECURITY MODEL (§5.9 decision, recorded in the MR):
 * - Client timers are UX ONLY. The security boundary is server-side:
 *   the access token dies at <=15 min and the refresh-token family is
 *   revoked through the EXISTING `logout()` path (`POST /auth/logout`,
 *   rotation + family revocation in application/auth.py). The client
 *   window below merely decides WHEN this tab volunteers that
 *   revocation; it can never extend a token's server-side life.
 * - The idle window is bounded by the server's refresh TTL
 *   (REFRESH_TOKEN_TTL_SECONDS = 14 days): a client timer longer than
 *   the refresh TTL could promise a session the server would refuse.
 *   15 minutes is far inside that bound and matches the house
 *   access-token custody posture (JWT <= 15 min).
 * - Multi-tab coherence rides the EXISTING custody model: the refresh
 *   token lives in per-tab sessionStorage (session.ts), so every tab
 *   owns — and revokes — its OWN token family independently; within a
 *   tab the session store's subscribe/notify flips `useSession()` and
 *   RequireAuth lands on the login gate. No cross-tab channel exists
 *   or is needed.
 * - Fail-closed: any timer/evaluation error triggers the sign-out
 *   path; `logout()` clears client credentials in its `finally` even
 *   when the revocation call dies at the wire.
 */

/** Idle window before this tab signs itself out. */
export const IDLE_WINDOW_SECONDS = 15 * 60;

/** Warning lead time — the alertdialog shows for the final minute. */
export const IDLE_WARNING_SECONDS = 60;

/**
 * Activity sampling throttle: pointer/key events are recorded at most
 * once per this interval (cheap listeners; the 1 s evaluation tick
 * gives the window its real resolution).
 */
export const ACTIVITY_THROTTLE_MS = 5_000;

/** Evaluation tick. */
export const IDLE_TICK_MS = 1_000;

/**
 * Tracked activity events. `visibilitychange` is handled separately —
 * returning to a tab is deliberately NOT activity (a tab hidden past
 * the window must land on the login gate immediately, not inherit a
 * fresh window).
 */
export const ACTIVITY_EVENTS = ["pointerdown", "pointermove", "keydown"] as const;
