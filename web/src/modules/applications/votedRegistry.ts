/**
 * Per-tab voted-application registry (W58-6 — honest "spent" vote
 * affordances).
 *
 * WHY THIS EXISTS: after a recorded vote the buttons were disabled via
 * component state only — switching agenda items and back remounted the
 * panel and RE-ARMED the buttons, steering the operator into a
 * guaranteed 409 (the server's one-vote-per-voter UNIQUE makes replay
 * harmless, but the re-armed affordance is dishonest). This registry
 * records the application ids THIS TAB has voted on so the affordance
 * stays spent across remounts within the session.
 *
 * Scope and honesty (the makerRegistry precedent): per-tab, in-memory —
 * it cannot know votes cast by another operator or in another tab; for
 * those the server's 409 remains the enforcer (least disclosure). Session-scoped
 * per W58-2: registered below, so both teardown paths clear it and the
 * next operator's tab never inherits a previous operator's spent votes.
 *
 * Storage: the shared createSessionScopedRegistry
 * primitive — teardown on both session-death paths and the reactive
 * read are wired by construction. This wrapper keeps the module's
 * exported vocabulary byte-compatible.
 */
import { createSessionScopedRegistry } from "@/modules/auth/createSessionScopedRegistry";

const voted = createSessionScopedRegistry<string, true>();

/** Record that THIS TAB cast a vote on `applicationId`. */
export function recordVotedApplication(applicationId: string): void {
  voted.set(applicationId, true);
}

/** Whether THIS TAB already voted on `applicationId`. */
export function hasVotedOn(applicationId: string): boolean {
  return voted.has(applicationId);
}

/** Session-teardown hygiene (W58-2): the registry is torn down by
 * construction; this named clear stays for test hygiene and callers. */
export function clearVotedApplications(): void {
  voted.clear();
}

/** Reactive read for the review panel (re-renders on record/clear). */
export function useHasVotedOn(applicationId: string): boolean {
  return voted.useHas(applicationId);
}
