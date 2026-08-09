/**
 * Per-tab dividend-declarer registry (separation of duties, UX side; the exits/applications makerRegistry precedent).
 *
 * DEMOTED TO A FALLBACK (the human-authorized read-contract expansion): DeclarationOut now exposes `requested_by`
 * (the migration-0020 column the server's declarer-cannot-vote /
 * declarer-cannot-distribute 403s already enforce) as the bare
 * nullable staff UUID — SERVER TRUTH, and it SUPERSEDES this per-tab
 * witness everywhere (the exits /0036 precedent). This registry is
 * consulted ONLY when the server record is unattributed
 * (requested_by === null — e.g. a system-actor declaration): for
 * those rows the tab's own witness is still honest signal; nothing is
 * ever invented for rows this tab did not witness.
 *
 * Scope and honesty: the registry is per-tab and in-memory — it cannot
 * know a declaration made by another operator or in another tab, and
 * for those the maker is UNKNOWN (a distinct sentinel that never
 * equals a real user id, so MakerCheckerPanel's self-check simply
 * cannot match). The ENFORCED invariants are server-side regardless
 * (least disclosure): both self-acts 403 on the persisted requested_by, and
 * the decision needs the configured quorum (the P9 machinery).
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
export const DIVIDEND_MAKER_UNKNOWN = "dividend-maker-unknown";

/** Record that `userId` declared dividend `declarationId`. */
export function recordDividendMaker(declarationId: string, userId: string): void {
  makers.set(declarationId, userId);
}

/** The maker this tab witnessed, or the UNKNOWN sentinel. */
export function dividendMakerOf(declarationId: string): string {
  return makers.get(declarationId) ?? DIVIDEND_MAKER_UNKNOWN;
}

/** Session-teardown hygiene (W58-2): the registry is torn down by
 * construction; this named clear stays for test hygiene and callers. */
export function clearDividendMakers(): void {
  makers.clear();
}
