/**
 * Per-tab voted-declaration registry (W58-6 — honest "spent" vote
 * affordances; the exits/applications votedRegistry precedent).
 *
 * WHY THIS EXISTS: after a recorded dividend vote, component state
 * alone would re-arm the vote buttons when the drawer remounts (close
 * and reopen the record), steering the operator into a guaranteed 409
 * (the server's one-vote-per-voter UNIQUE makes replay harmless, but
 * the re-armed affordance is dishonest). This registry records the
 * declaration ids THIS TAB has voted on so the affordance stays spent
 * across remounts within the session.
 *
 * Scope and honesty: per-tab, in-memory — it cannot know votes cast by
 * another operator or in another tab; for those the server's 409
 * remains the enforcer (least disclosure). Session-scoped per W58-2:
 * registered below, so both teardown paths clear it and the next
 * operator's tab never inherits a previous operator's spent votes.
 *
 * RESOLVED: the S3 disposition assigned the
 * `createSessionScopedRegistry` consolidation to the first batch
 * landing after both in-flight batches — delivered it; this
 * wrapper now consumes the shared primitive.
 *
 * Storage: the shared createSessionScopedRegistry primitive — teardown
 * on both session-death paths and the reactive read are wired by
 * construction. This wrapper keeps the module's exported vocabulary
 * byte-compatible.
 */
import { createSessionScopedRegistry } from "@/modules/auth/createSessionScopedRegistry";

const voted = createSessionScopedRegistry<string, true>();

/** Record that THIS TAB cast a vote on declaration `declarationId`. */
export function recordVotedDeclaration(declarationId: string): void {
  voted.set(declarationId, true);
}

/** Whether THIS TAB already voted on declaration `declarationId`. */
export function hasVotedOnDeclaration(declarationId: string): boolean {
  return voted.has(declarationId);
}

/** Session-teardown hygiene (W58-2): the registry is torn down by
 * construction; this named clear stays for test hygiene and callers. */
export function clearVotedDeclarations(): void {
  voted.clear();
}

/** Reactive read for the detail drawer (re-renders on record/clear). */
export function useHasVotedOnDeclaration(declarationId: string): boolean {
  return voted.useHas(declarationId);
}
