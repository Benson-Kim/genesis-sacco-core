/**
 * Per-tab exit maker registry (module 7 — separation of duties, UX side; the applications makerRegistry precedent).
 *
 * STATUS (follow-up on / migration 0036): the limitation
 * this registry originally worked around is CLOSED for attributed
 * rows — ExitOut now carries `requested_by` (the initiator, as the
 * bare staff UUID), and SERVER TRUTH SUPERSEDES this per-tab witness
 * wherever it is present (ExitDetailDrawer and SettleExitDialog
 * consume `requested_by` first). Pre-0036 rows without unambiguous
 * audit history are honestly NULL on the contract (attribution is
 * never invented), so this registry remains the fallback witness for
 * exactly those: when the signed-in operator creates an exit request,
 * their own user id is recorded against the new exit record, and it
 * is consulted only when the server record is unattributed
 * (`requested_by === null`).
 *
 * Scope and honesty: the registry is per-tab and in-memory — it cannot
 * know a request initiated by another operator or in another tab, and
 * for those the maker is UNKNOWN (a distinct sentinel that never
 * equals a real user id, so MakerCheckerPanel's self-check simply
 * cannot match). The ENFORCED invariants are server-side regardless
 * (least disclosure): the initiator can never vote on their own request and
 * can never post its money-moving settlement (both 403, keyed on the
 * persisted requested_by), and the decision needs a two-voter quorum
 * (the P9 machinery).
 *
 * Storage: the shared createSessionScopedRegistry
 * primitive — teardown on BOTH session-death paths (the query-path 401
 * dual-cache teardown and explicit sign-out) is wired by construction
 * (W58-2, the F2 class). This wrapper keeps the module's exported
 * vocabulary byte-compatible.
 */
import { createSessionScopedRegistry } from "@/modules/auth/createSessionScopedRegistry";

const makers = createSessionScopedRegistry<string, string>();

/** Sentinel for "maker not witnessed by this tab" — never a valid UUID. */
export const EXIT_MAKER_UNKNOWN = "exit-maker-unknown";

/** Record that `userId` initiated exit request `exitId`. */
export function recordExitMaker(exitId: string, userId: string): void {
  makers.set(exitId, userId);
}

/** The maker this tab witnessed, or the UNKNOWN sentinel. */
export function exitMakerOf(exitId: string): string {
  return makers.get(exitId) ?? EXIT_MAKER_UNKNOWN;
}

/** Session-teardown hygiene (W58-2): the registry is torn down by
 * construction; this named clear stays for test hygiene and callers. */
export function clearExitMakers(): void {
  makers.clear();
}
