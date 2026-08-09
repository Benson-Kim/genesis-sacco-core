/**
 * Per-tab committee maker registry (module 3 — separation of duties, UX side).
 *
 * STATUS (close-out, migration 0037): the limitation this
 * registry worked around is CLOSED for attributed rows — ApplicationOut
 * carries `created_by` (the initiator, /0036) AND `recommended_by`
 * (the committee referrer, 0037), both as the bare staff UUID, and
 * SERVER TRUTH SUPERSEDES this per-tab witness wherever either is
 * present (CommitteeScreen consumes the server attribution first).
 * The registry's residual role is the fallback witness for exactly the
 * rows the server honestly cannot attribute (`recommended_by === null`
 * AND `created_by === null` — pre-0036/0037 history that was not
 * unambiguous, or system moves; attribution is never invented): when
 * the signed-in operator performs the "recommend to committee"
 * transition, their own user id is recorded against the application,
 * and it is consulted only for those unattributed rows.
 *
 * Scope and honesty: the registry is per-tab and in-memory — it cannot
 * know a recommendation made by another operator or in another tab, and
 * for those the maker is UNKNOWN (a distinct sentinel that never equals
 * a real user id, so MakerCheckerPanel's self-check simply cannot match).
 * The ENFORCED invariants are server-side regardless (least disclosure): one
 * vote per voter (DB UNIQUE), voting open only in committee stage,
 * approval-authority bands, the RECOMMENDER can never vote on the
 * application they referred (403, keyed on the persisted
 * recommended_by — 0037), and neither the initiator nor the recommender
 * can post the disbursement (403, keyed on the persisted
 * created_by/recommended_by).
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
export const MAKER_UNKNOWN = "maker-unknown";

/** Record that `userId` moved `applicationId` into committee. */
export function recordCommitteeMaker(applicationId: string, userId: string): void {
  makers.set(applicationId, userId);
}

/** The maker this tab witnessed, or the UNKNOWN sentinel. */
export function committeeMakerOf(applicationId: string): string {
  return makers.get(applicationId) ?? MAKER_UNKNOWN;
}

/** Session-teardown hygiene (W58-2): the registry is torn down by
 * construction; this named clear stays for test hygiene and callers. */
export function clearCommitteeMakers(): void {
  makers.clear();
}
