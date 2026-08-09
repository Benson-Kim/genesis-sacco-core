import { z } from "zod";
import { isoTimestampSchema, moneySchema, MONEY_RE } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the Dividends lifecycle console
 * (the declare/approve/distribute contract, incl. the issue- dormant/unclaimed policy surface of migrations 0020/0022). Shapes mirror the generated client types
 * (components["schemas"]["DeclarationOut"], DividendVoteResultOut,
 * DistributionRunOut) — the drift-checked OpenAPI snapshot remains the
 * contract; these schemas only assert it at runtime.
 *
 * MONEY RULE: every figure on a declaration (bases,
 * totals) and every distribution-run total is a SERVER-computed
 * decimal STRING rendered verbatim via fmtKes — never summed, netted
 * or re-derived client-side (the client does not even verify that
 * total_payout equals dividend + rebate; that equality is migration
 * 0020's ck_dividend_declarations_totals CHECK, enforced at the
 * database).
 *
 * Field-by-field shape derivation (backend/src/genesis, not guessed):
 * - total_share_basis, total_deposit_basis, total_dividend,
 *   total_rebate, total_payout: numeric(18,2) columns with CHECK
 *   (>= 0) — total_payout CHECK (> 0) — serialised via str(Decimal)
 *   (api/dividends.py:_declaration_out) → canonical two-place shape,
 *   no sign branch anywhere: `moneySchema`.
 * - dividend_rate_pct / rebate_rate_pct: numeric(5,2) columns with
 *   CHECK (0..100) — the same str(Decimal) two-place shape; they are
 *   RATES, not money (rendered as "% p.a.", never via fmtKes), so
 *   they get their own alias of the canonical decimal shape below.
 * - DistributionRunOut dividend_total, rebate_total, payout_total:
 *   sums seeded from domain/money.py ZERO = Decimal("0.00") over
 *   to_cents-quantised claims, payout_total itself re-quantised via
 *   to_cents (application/dividends.py DistributionRunResult) — the
 *   scale-2 seed means even an all-skipped run serialises "0.00",
 *   never the bare aggregate "0": `moneySchema`, not
 *   aggregateMoneySchema.
 * - fy_start / fy_end: DATE columns serialised via date.isoformat()
 *   → "YYYY-MM-DD" (`fyDateSchema`), NOT datetimes.
 * - decided_at / distributed_at / created_at: datetime.isoformat()
 *   (`isoTimestampSchema`), the two decision stamps nullable.
 */

/** Mirrors the backend DeclarationStatus enum
 * (application/dividends.py). An unknown value is a contract
 * violation and is REJECTED, never rendered — an unrecognised status
 * must never arm a lifecycle write. Terminal states: rejected,
 * distributed (void narrows declared/approved → rejected; there is no
 * separate "voided" status in the contract). */
export const DECLARATION_STATUSES = ["declared", "approved", "rejected", "distributed"] as const;
export const declarationStatusSchema = z.enum(DECLARATION_STATUSES);
export type DeclarationStatus = z.infer<typeof declarationStatusSchema>;

export const DECLARATION_STATUS_LABELS: Record<DeclarationStatus, string> = {
  declared: "Declared",
  approved: "Approved",
  rejected: "Rejected",
  distributed: "Distributed",
};

/** The terminal states: every write affordance is structurally
 * WITHDRAWN for them (the server enforces the transition matrix regardless — least disclosure). */
export const TERMINAL_DECLARATION_STATUSES: readonly DeclarationStatus[] = [
  "rejected",
  "distributed",
];

export function isTerminalDeclarationStatus(status: DeclarationStatus): boolean {
  return TERMINAL_DECLARATION_STATUSES.includes(status);
}

/** numeric(5,2) rate column (CHECK 0..100) serialised via
 * str(Decimal): the canonical two-place decimal shape. A RATE, not
 * money — rendered as "% p.a.", never via fmtKes; shares the shape
 * assertion only. */
export const rateSchema = z.string().regex(MONEY_RE);

/** DATE column via date.isoformat(): "YYYY-MM-DD" exactly (the FY
 * bounds are calendar dates, not instants — feeding these to
 * fmtDateTime would be dishonest about precision). */
export const FY_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const fyDateSchema = z.string().regex(FY_DATE_RE);

/**
 * One dividend declaration (DeclarationOut): the committee-approval
 * SNAPSHOT for the last completed financial year. Every figure is
 * server-computed under the machinery from tenant-configured
 * rates (no rate/period/total can even be SENT by a
 * client — extra="forbid" server-side). Extra keys are STRIPPED at
 * this boundary.
 *
 * ATTRIBUTION (the human-authorized read-contract expansion closing the gap): DeclarationOut
 * now exposes `requested_by` — the EXISTING migration-0020 column
 * that already drives the server's declarer-cannot-vote /
 * declarer-cannot-distribute 403s — as the bare staff UUID under
 * least disclosure (the / exits precedent). NULLABLE, never
 * optional (the key must be present; a missing key is contract
 * drift and REFUSES to parse), and never invented: NULL is the
 * honest unattributed affordance. SERVER TRUTH SUPERSEDES the
 * per-tab maker witness (makerRegistry.ts — demoted to a fallback
 * for unattributed rows only).
 */
export const declarationSchema = z.object({
  id: z.string(),
  fy_start: fyDateSchema,
  fy_end: fyDateSchema,
  dividend_rate_pct: rateSchema,
  rebate_rate_pct: rateSchema,
  /** Members with a positive entitlement at declaration time —
   * DORMANT members included (P1, migration 0022: dormant members remain shareholders and are IN the scan population). */
  eligible_members: z.number().int(),
  total_share_basis: moneySchema,
  total_deposit_basis: moneySchema,
  total_dividend: moneySchema,
  total_rebate: moneySchema,
  /** CHECK (> 0): an empty declaration is unrepresentable (0020). */
  total_payout: moneySchema,
  status: declarationStatusSchema,
  /** Declarer attribution: the SERVER's bare
   * staff UUID (0020 column), nullable-NOT-optional; rendered via the
   * short-id convention, NULL = honest "unattributed". Server truth —
   * it supersedes the per-tab witness wherever both exist. */
  requested_by: z.string().nullable(),
  decided_at: isoTimestampSchema.nullable(),
  distributed_at: isoTimestampSchema.nullable(),
  /** Optimistic-lock version pinning the void write (1.4). */
  version: z.number().int(),
  created_at: isoTimestampSchema,
});

export type DeclarationRecord = z.infer<typeof declarationSchema>;

/** Committee tally after a dividend vote (DividendVoteResultOut — the P9 voting machinery reused by): COUNTS + decision + the
 * declaration's resulting status only; the server never discloses who
 * voted which way through this endpoint. `decision` and `status`
 * REJECT unknown vocabulary (W58-4 posture). */
export const dividendVoteResultSchema = z.object({
  approvals: z.number().int(),
  rejections: z.number().int(),
  decision: z.enum(["approved", "rejected"]).nullable(),
  status: declarationStatusSchema,
});

export type DividendVoteResult = z.infer<typeof dividendVoteResultSchema>;

export const DIVIDEND_VOTES = ["approve", "reject"] as const;
export type DividendVote = (typeof DIVIDEND_VOTES)[number];

/**
 * Distribution-run result (DistributionRunOut): the mass money-mover's
 * report. Counts are SERVER facts about the run; the three totals are
 * server Decimal strings (see the module docblock's ZERO-seed
 * derivation — always two places). `unclaimed` is the issue- P3
 * dormant-policy surface: members who EXITED mid-run, disposed as an
 * unclaimed-dividends PAYABLE (0022 disposition column), never
 * silently dropped and never credited to a closed account.
 * `pending_members > 0` with status still "approved" means members
 * were concurrency-skipped (SKIP LOCKED) — the run is designed to be
 * idempotently RE-RUN to pick them up; the declaration is marked
 * distributed only when the claims reconcile exactly with the
 * approved snapshot.
 */
export const distributionRunSchema = z.object({
  scanned: z.number().int(),
  claimed: z.number().int(),
  skipped_zero: z.number().int(),
  skipped_claimed: z.number().int(),
  unclaimed: z.number().int(),
  dividend_total: moneySchema,
  rebate_total: moneySchema,
  payout_total: moneySchema,
  pending_members: z.number().int(),
  batches: z.number().int(),
  status: declarationStatusSchema,
});

export type DistributionRun = z.infer<typeof distributionRunSchema>;
