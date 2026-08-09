import { z } from "zod";
import { isoTimestampSchema, moneySchema, signedMoneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the Member exit module (module 7 — the exit & settlement API). Shapes mirror the
 * generated client types (components["schemas"]["ExitOut"],
 * ExitStatementOut, ExitVoteResultOut, SettlementOut) — the
 * drift-checked OpenAPI snapshot remains the contract; these schemas
 * only assert it at runtime.
 *
 * MONEY RULE — the most money-critical surface of
 * the batch order (terminal settlement: share refunds, loan offsets,
 * fee deductions): every settlement figure (shares_amount,
 * deposits_amount, loan_balance, fees, net_payable, equity) is a
 * SERVER-computed decimal STRING snapshotted under the lock set.
 * They render verbatim via fmtKes and are NEVER coerced, combined,
 * netted or re-derived client-side — the client does not even verify
 * that net_payable equals its components (that re-verification is the
 * server's, component-by-component, at posting time). A NEGATIVE
 * net_payable (loan balance exceeds assets) is a documented, legitimate
 * server branch and renders verbatim with its sign.
 */

/** Mirrors the backend ExitStatus enum (domain/exits.py). An unknown
 * value is a contract violation and is REJECTED, never rendered — an
 * unrecognised status must never arm a lifecycle write. Terminal
 * states: settled, rejected (void narrows requested/approved →
 * rejected; there is no separate "voided" status in the contract). */
export const EXIT_STATUSES = ["requested", "approved", "settled", "rejected"] as const;
export const exitStatusSchema = z.enum(EXIT_STATUSES);
export type ExitStatus = z.infer<typeof exitStatusSchema>;

export const EXIT_STATUS_LABELS: Record<ExitStatus, string> = {
  requested: "Requested",
  approved: "Approved",
  settled: "Settled",
  rejected: "Rejected",
};

/** The terminal states: every write affordance is structurally
 * WITHDRAWN for them (the server enforces the transition matrix regardless — least disclosure). */
export const TERMINAL_EXIT_STATUSES: readonly ExitStatus[] = ["settled", "rejected"];

export function isTerminalExitStatus(status: ExitStatus): boolean {
  return TERMINAL_EXIT_STATUSES.includes(status);
}

/**
 * ISO-8601 datetime SHAPE for the exit timestamps (an earlier review lesson) and canonical SERVER money decimal SHAPE (review):
 * SHARED shapes from `@/lib/schemas` — the single copy (reuse-first); this module was their reference implementation.
 *
 * The sign: shares_amount, deposits_amount, loan_balance and fees
 * carry DB CHECK (>= 0) constraints (migrations 0001/0010) and equity
 * is the server sum of two non-negative components — a '-' on any of
 * them is a contract violation and is REJECTED (`moneySchema`).
 * net_payable deliberately has NO such CHECK (migration 0010: the
 * documented loan-exceeds-assets branch) — it alone accepts a leading
 * '-' (`signedMoneySchema`) and renders verbatim with its sign.
 */
const timestampSchema = isoTimestampSchema;

/** One exit request with its settlement SNAPSHOT (ExitOut). Every
 * money field is a decimal string in the CANONICAL server shape
 *  — a numeric value or a garbage string is a contract
 * violation and is REJECTED. Extra keys are STRIPPED at this
 * boundary. */
export const exitSchema = z.object({
  id: z.string(),
  member_id: z.string(),
  status: exitStatusSchema,
  reason: z.string().nullable(),
  /** Initiator attribution (migration 0036 — the follow-up): the staff principal who requested the exit, as the
   * BARE user UUID only. Least disclosure: never a name or
   * email — resolving the id stays behind access_control:view
   * server-side, and this client NEVER fetches a directory record for
   * it. NULL is an honest "unattributed" (pre-0036 rows without
   * unambiguous audit history —: an actor is never invented). */
  requested_by: z.string().nullable(),
  /** Snapshot components, server-computed under the lock set. */
  shares_amount: moneySchema,
  deposits_amount: moneySchema,
  loan_balance: moneySchema,
  fees: moneySchema,
  /** May legitimately be NEGATIVE (loan balance exceeds assets). */
  net_payable: signedMoneySchema,
  decided_at: timestampSchema.nullable(),
  settled_at: timestampSchema.nullable(),
  settlement_txn_id: z.string().nullable(),
  /** Optimistic-lock version pinning void/settlement writes (1.4). */
  version: z.number().int(),
  created_at: timestampSchema,
});

export type ExitRecord = z.infer<typeof exitSchema>;

/** Committee tally after an exit vote (ExitVoteResultOut — the P9 voting machinery reused by): COUNTS + decision + the record's
 * resulting status only; the server never discloses who voted which
 * way through this endpoint. `decision` and `status` REJECT unknown
 * vocabulary (W58-4 posture). */
export const exitVoteResultSchema = z.object({
  approvals: z.number().int(),
  rejections: z.number().int(),
  decision: z.enum(["approved", "rejected"]).nullable(),
  status: exitStatusSchema,
});

export type ExitVoteResult = z.infer<typeof exitVoteResultSchema>;

export const EXIT_VOTES = ["approve", "reject"] as const;
export type ExitVote = (typeof EXIT_VOTES)[number];

/** Settlement posting result (SettlementOut): the updated exit record
 * plus the ledger transaction ref — SERVER facts, verbatim. */
export const settlementResultSchema = z.object({
  exit: exitSchema,
  txn_ref: z.string().nullable(),
});

export type SettlementResult = z.infer<typeof settlementResultSchema>;

/** Mirrors the backend MemberStatus enum for the statement's
 * member_status field — reject-unknowns, consistent with the members
 * module boundary. */
const statementMemberStatusSchema = z.enum(["active", "arrears", "dormant", "exited"]);

/**
 * The canonical exit-statement document (ExitStatementOut,
 * `GET /member-exits/{id}/statement`) — a financial DOCUMENT: every
 * figure and line renders VERBATIM; nothing is recomputed, summed or
 * netted client-side (equity is the SERVER's shares+deposits figure —
 * the client never re-derives it from the components). The CSV/PDF
 * export of this document ships via the reports module
 * (`member_exit_statement`, blocker (k)) — no download affordance
 * is duplicated here, so no filename is ever derived from a server
 * string (the class cannot arise).
 */
export const exitStatementSchema = z.object({
  exit_id: z.string(),
  member_id: z.string(),
  member_no: z.string(),
  member_name: z.string(),
  member_status: statementMemberStatusSchema,
  exit_status: exitStatusSchema,
  reason: z.string().nullable(),
  /** Initiator attribution on the canonical DOCUMENT (follow-up): the same bare staff UUID as ExitOut.requested_by,
   * rendered verbatim via the short-id convention — never resolved to
   * a name/email; NULL stays the honest unattributed line
   * */
  requested_by: z.string().nullable(),
  shares_amount: moneySchema,
  deposits_amount: moneySchema,
  /** SERVER-computed equity line — never re-derived client-side. */
  equity: moneySchema,
  loan_balance: moneySchema,
  fees: moneySchema,
  net_payable: signedMoneySchema,
  requested_at: timestampSchema,
  decided_at: timestampSchema.nullable(),
  settled_at: timestampSchema.nullable(),
  settlement_txn_ref: z.string().nullable(),
});

export type ExitStatement = z.infer<typeof exitStatementSchema>;

/**
 * Client-side pre-validation of the exit-request form (the server re-validates and is the enforcer — least disclosure). NO money field exists
 * anywhere in this input: the contract computes every settlement
 * figure server-side under locks and the exit fee comes exclusively
 * from tenant configuration (`extra="forbid"` server-side) — nothing
 * money-shaped can even be sent.
 */
export const exitRequestEntrySchema = z.object({
  member_id: z.string().min(1, "Select a member."),
  /** Optional free-text reason, mirroring the contract's max length. */
  reason: z.string().max(200, "Keep the reason under 200 characters."),
});

export type ExitRequestEntry = z.infer<typeof exitRequestEntrySchema>;

/**
 * Advisory exit-eligibility facts (ExitEligibilityOut, `GET /member-exits/eligibility/{member_id}` —,: the prototype's vExit() criteria rows, surfaced BEFORE a request is submitted).
 *
 * COUNTS AND BOOLEANS ONLY — the contract carries NO amount (least disclosure, least disclosure), so no money shape exists at this boundary and
 * nothing here may ever feed fmtKes. KEY-EXACT and REQUIRED: no field
 * in this contract is nullable — a missing key is a contract violation
 * and is REJECTED, never defaulted (nullable-not-optional posture; here
 * that means neither is allowed). ADVISORY only: the read is computed
 * WITHOUT locks — the BINDING verdict stays with the server's locked
 * recomputes at request time and again at settlement; the client
 * renders the facts verbatim and NEVER re-derives the verdict from the
 * component facts (advisory_eligible is the server's own conjunction).
 */
export const exitEligibilitySchema = z.object({
  member_id: z.string(),
  /** Reject-unknowns, mirroring the members boundary: an unrecognised
   * status must never label a checklist row. */
  member_status: statementMemberStatusSchema,
  status_allows_exit: z.boolean(),
  open_exit_exists: z.boolean(),
  /** Counts are non-negative INTEGERS — a float, a negative or a
   * stringified number is a contract violation and is REJECTED (it
   * would silently mislabel a blocker row). */
  live_guarantees_count: z.number().int().nonnegative(),
  open_applications_count: z.number().int().nonnegative(),
  /** Informational, NOT a blocker — active loans net within the
   * settlement (the early-settlement rule). */
  active_loans_count: z.number().int().nonnegative(),
  unresolved_writeoff_claim: z.boolean(),
  advisory_eligible: z.boolean(),
  as_of: timestampSchema,
});

export type ExitEligibility = z.infer<typeof exitEligibilitySchema>;
