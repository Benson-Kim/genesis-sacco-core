import { z } from "zod";
import { isoTimestampSchema, moneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the Corrections module (follow-on, — the corrections API: repayment adjustments, misc fees, loan write-offs and the issue- recovery receipts). Shapes mirror the generated client types
 * (components["schemas"]["AdjustmentRecordOut"], AdjustmentOut, FeeOut,
 * WriteOffOut, WriteOffVoteResultOut, WriteOffPostOut,
 * RecoveryReceiptOut, RecoveryReceiptRowOut, RecoveryReceiptsOut) —
 * the drift-checked OpenAPI snapshot remains the contract; these
 * schemas only assert it at runtime.
 *
 * MONEY RULE — corrections are the FRAUD CHANNEL:
 * every figure on this surface is a SERVER-computed decimal STRING
 * (reversal legs derived from the original transaction's append-only legs, write-off figures from the write-once 0025 snapshot, fee amounts from tenant configuration, recovery positions from the snapshot + append-only receipts under the write-off row lock). They
 * render verbatim via fmtKes and are NEVER coerced, combined, netted or
 * re-derived client-side — the client does not even verify that
 * amount equals the sum of its three components (that identity is the
 * 0025 ck_repayment_adjustments_components CHECK, and posting-time
 * re-verification is the server's).
 *
 * THE SIGN (moneySchema vs signedMoneySchema, chosen per field from the migration CHECK constraints — the convention):
 * - repayment_adjustments (0025): amount CHECK (> 0), penalties /
 *   interest / principal CHECK (>= 0); 0031 adds
 *   loan_balance_at_request and loan_penalty_due_at_request, CHECK
 *   (>= 0) each. AdjustmentOut's balance_after and penalty_due_after
 *   are the recomputed loans.balance CHECK (>= 0) (0001) and
 *   loans.penalty_due CHECK (>= 0) (0007).
 * - loan_write_offs (0025): balance CHECK (>= 0), penalty_due CHECK
 *   (>= 0), total_written_off CHECK (> 0).
 * - loan_recoveries (0030): amount CHECK (> 0); recovered_total /
 *   outstanding_claim are to_cents-quantised server sums bounded by
 *   the 0030/0034 over-recovery guards (receipts never exceed the
 *   claim), so both are non-negative canonical decimals.
 * NO field in this module has a documented negative branch — a '-'
 * anywhere is a contract violation and is REJECTED (`moneySchema`
 * everywhere; `signedMoneySchema` deliberately unused).
 */

/** Mirrors the backend AdjustmentStatus enum
 * (application/corrections.py, the 0031 workflow machine). An unknown
 * value is a contract violation and is REJECTED, never rendered — an
 * unrecognised status must never arm a checker write. */
export const ADJUSTMENT_STATUSES = ["pending_approval", "posted", "rejected"] as const;
export const adjustmentStatusSchema = z.enum(ADJUSTMENT_STATUSES);
export type AdjustmentStatus = z.infer<typeof adjustmentStatusSchema>;

export const ADJUSTMENT_STATUS_LABELS: Record<AdjustmentStatus, string> = {
  pending_approval: "Pending approval",
  posted: "Posted",
  rejected: "Rejected",
};

/** Terminal adjustment states: every checker affordance is
 * structurally WITHDRAWN for them (the server enforces the 0031 transition matrix regardless — least disclosure). */
export const TERMINAL_ADJUSTMENT_STATUSES: readonly AdjustmentStatus[] = ["posted", "rejected"];

export function isTerminalAdjustmentStatus(status: AdjustmentStatus): boolean {
  return TERMINAL_ADJUSTMENT_STATUSES.includes(status);
}

/** Mirrors the backend WriteOffStatus enum (application/corrections.py
 * / the 0025 status CHECK). Reject-unknowns, same posture. */
export const WRITE_OFF_STATUSES = ["requested", "approved", "rejected", "posted"] as const;
export const writeOffStatusSchema = z.enum(WRITE_OFF_STATUSES);
export type WriteOffStatus = z.infer<typeof writeOffStatusSchema>;

export const WRITE_OFF_STATUS_LABELS: Record<WriteOffStatus, string> = {
  requested: "Requested",
  approved: "Approved",
  rejected: "Rejected",
  posted: "Posted",
};

/** Terminal write-off states (rejected frees the per-loan slot; posted
 * is money history): vote/void/post affordances are structurally
 * WITHDRAWN — only the recovery-receipt affordances survive a POSTED
 * write-off. */
export const TERMINAL_WRITE_OFF_STATUSES: readonly WriteOffStatus[] = ["rejected", "posted"];

export function isTerminalWriteOffStatus(status: WriteOffStatus): boolean {
  return TERMINAL_WRITE_OFF_STATUSES.includes(status);
}

/** The prudential NPL classes a write-off snapshot may carry (the 0025
 * ck backstop: a snapshot of a performing loan is unrepresentable) —
 * a strict SUBSET of the loans module's LoanClass vocabulary. */
export const WRITE_OFF_CLASSES = ["substandard", "doubtful", "loss"] as const;
export const writeOffClassSchema = z.enum(WRITE_OFF_CLASSES);
export type WriteOffClass = z.infer<typeof writeOffClassSchema>;

/** Mirrors the backend committee Vote enum (domain/committee.py). */
export const WRITE_OFF_VOTES = ["approve", "reject"] as const;
export type WriteOffVote = (typeof WRITE_OFF_VOTES)[number];

/** Mirrors the backend FeeType enum (application/corrections.py):
 * code-owned fee vocabulary — each member maps to the settings
 * key its amount is resolved from. The caller can only
 * ever name a type from this enum, never an amount. */
export const FEE_TYPES = ["registration"] as const;
export const feeTypeSchema = z.enum(FEE_TYPES);
export type FeeType = z.infer<typeof feeTypeSchema>;

export const FEE_TYPE_LABELS: Record<FeeType, string> = {
  registration: "Registration fee",
};

/**
 * One adjustment workflow row (AdjustmentRecordOut): the
 * pending request a checker reviews, or the terminal posted/rejected
 * history. Every money field is a decimal string in the CANONICAL
 * server shape; extra keys are STRIPPED at this boundary.
 *
 * ATTRIBUTION (the pattern): maker_id is the CONTRACT's maker —
 * server truth, REQUIRED (0025: maker_id NOT NULL); checker_id is
 * nullable (undecided rows). Both render under LEAST DISCLOSURE as the
 * bare staff UUID via the short-id convention — never a name or email
 * (resolving the id stays behind access_control:view server-side, and
 * this client NEVER fetches a directory record for it).
 */
export const adjustmentSchema = z.object({
  id: z.string(),
  repayment_id: z.string(),
  loan_id: z.string(),
  original_transaction_id: z.string(),
  reversal_transaction_id: z.string().nullable(),
  /** The maker principal — REQUIRED server truth (0025 NOT NULL). */
  maker_id: z.string(),
  /** The checker principal — NULL until a decision (nullable, never
   * optional: dropping the key is a contract violation). */
  checker_id: z.string().nullable(),
  reason: z.string(),
  /** The COMPLETE original allocation being undone (0025: amount
   * CHECK (> 0) equal to the sum of the three component legs, each
   * CHECK (>= 0)); the identity is the DB's — never re-verified
   * client-side. */
  amount: moneySchema,
  penalties: moneySchema,
  interest: moneySchema,
  principal: moneySchema,
  would_reopen_loan: z.boolean(),
  status: adjustmentStatusSchema,
  /** 0031 approval snapshot (CHECK >= 0 each; NULL on pre-0031 rows). */
  loan_balance_at_request: moneySchema.nullable(),
  loan_penalty_due_at_request: moneySchema.nullable(),
  loan_status_at_request: z.string().nullable(),
  decided_at: isoTimestampSchema.nullable(),
  /** Optimistic-lock version pinning the reject write (1.4). */
  version: z.number().int(),
  created_at: isoTimestampSchema,
});

export type AdjustmentRecord = z.infer<typeof adjustmentSchema>;

/** Approval result (AdjustmentOut): the posted reversal — every figure
 * is the SERVER's (reversal legs + recomputed loan state), verbatim. */
export const adjustmentResultSchema = z.object({
  adjustment_id: z.string(),
  reversal_txn_id: z.string(),
  reversal_txn_ref: z.string(),
  amount: moneySchema,
  penalties: moneySchema,
  interest: moneySchema,
  principal: moneySchema,
  balance_after: moneySchema,
  penalty_due_after: moneySchema,
  loan_status: z.string(),
  reopened: z.boolean(),
});

export type AdjustmentResult = z.infer<typeof adjustmentResultSchema>;

/** Misc-fee posting result (FeeOut): the FE- ledger row — the amount
 * is the SERVER's -configured figure, verbatim (no fee amount
 * ever travels in a request). */
export const feeResultSchema = z.object({
  txn_id: z.string(),
  txn_ref: z.string(),
  fee_type: feeTypeSchema,
  amount: moneySchema,
});

export type FeeResult = z.infer<typeof feeResultSchema>;

/**
 * The write-once write-off snapshot (WriteOffOut — 0025):
 * the figures the committee approves, re-verified component-by-
 * component at posting (409 on drift, posting nothing).
 *
 * ATTRIBUTION HONESTY: the 0025 table carries requested_by, but
 * WriteOffOut deliberately does NOT serialise it — the requester is
 * NOT on this contract (recorded as a contract follow-up on). The per-tab makerRegistry is therefore the ONLY witness for
 * the SoD affordances here (the fallback pattern); the server
 * enforces requester-can't-vote/post regardless (least disclosure).
 */
export const writeOffSchema = z.object({
  id: z.string(),
  loan_id: z.string(),
  member_id: z.string(),
  /** Snapshot components (0025: balance and penalty_due CHECK (>= 0);
   * total_written_off CHECK (> 0) is their sum — the identity is the
   * DB's ck_loan_write_offs_totals, never re-verified client-side). */
  balance: moneySchema,
  penalty_due: moneySchema,
  total_written_off: moneySchema,
  classification: writeOffClassSchema,
  /** Percentage string (numeric(5,2), 0–100) — never fmtKes-fed; the
   * shape is deliberately NOT money-asserted (the loans-module
   * rate_pct precedent). */
  provision_pct: z.string(),
  reason: z.string(),
  status: writeOffStatusSchema,
  decided_at: isoTimestampSchema.nullable(),
  posted_at: isoTimestampSchema.nullable(),
  transaction_id: z.string().nullable(),
  /** Optimistic-lock version pinning the void write (1.4). */
  version: z.number().int(),
  created_at: isoTimestampSchema,
});

export type WriteOffRecord = z.infer<typeof writeOffSchema>;

/** Committee tally after a write-off vote (WriteOffVoteResultOut — the
 * P9 machinery): COUNTS + decision + resulting status only; the server
 * never discloses who voted which way through this endpoint.
 * `decision` and `status` REJECT unknown vocabulary. */
export const writeOffVoteResultSchema = z.object({
  approvals: z.number().int(),
  rejections: z.number().int(),
  decision: z.enum(["approved", "rejected"]).nullable(),
  status: writeOffStatusSchema,
});

export type WriteOffVoteResult = z.infer<typeof writeOffVoteResultSchema>;

/** Posting result (WriteOffPostOut): the WO- provisioning posting —
 * SERVER facts, verbatim. */
export const writeOffPostResultSchema = z.object({
  write_off_id: z.string(),
  loan_id: z.string(),
  txn_id: z.string().nullable(),
  txn_ref: z.string().nullable(),
  total_written_off: moneySchema,
  status: writeOffStatusSchema,
});

export type WriteOffPostResult = z.infer<typeof writeOffPostResultSchema>;

/** Recovery receipt result (RecoveryReceiptOut): the RC-
 * posting plus the reconstructed claim position, all SERVER figures
 * verbatim (recovered_total and outstanding_claim computed under the
 * write-off row lock — never client math). */
export const recoveryReceiptResultSchema = z.object({
  recovery_id: z.string(),
  write_off_id: z.string(),
  loan_id: z.string(),
  member_id: z.string(),
  txn_id: z.string(),
  txn_ref: z.string(),
  amount: moneySchema,
  recovered_total: moneySchema,
  outstanding_claim: moneySchema,
  claim_fully_recovered: z.boolean(),
  guarantees_released: z.number().int(),
  recovery_case_id: z.string().nullable(),
});

export type RecoveryReceiptResult = z.infer<typeof recoveryReceiptResultSchema>;

/** One receipt row (RecoveryReceiptRowOut). recorded_by is the
 * CONTRACT's actor attribution — REQUIRED server truth, rendered under
 * least disclosure (bare UUID, short-id convention). */
export const recoveryReceiptRowSchema = z.object({
  id: z.string(),
  amount: moneySchema,
  txn_id: z.string(),
  recovery_case_id: z.string().nullable(),
  recorded_by: z.string(),
  created_at: isoTimestampSchema,
});

export type RecoveryReceiptRow = z.infer<typeof recoveryReceiptRowSchema>;

/** Receipts page (RecoveryReceiptsOut): the keyset page PLUS the
 * server-reconstructed recovered/outstanding position — deliberately
 * NOT the shared keysetPageSchema (this contract carries two extra
 * money aggregates alongside the page). */
export const recoveryReceiptsPageSchema = z.object({
  items: z.array(recoveryReceiptRowSchema),
  recovered_total: moneySchema,
  outstanding_claim: moneySchema,
  next_cursor: z.string().nullable(),
});

export type RecoveryReceiptsPage = z.infer<typeof recoveryReceiptsPageSchema>;

/**
 * Client-side pre-validation of the workbench entry forms (the server re-validates and is the enforcer — least disclosure). NO money field exists
 * in ANY of these inputs: the contract derives every figure
 * server-side — the only caller-chosen values are ids,
 * reasons, the code-owned fee type, the vote and the CASH channel.
 */
const UUID_MSG = "Enter the full UUID (8-4-4-4-12 hex).";
const uuidField = (label: string) =>
  z
    .string()
    .min(1, `${label} is required.`)
    .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i, UUID_MSG);

export const adjustmentRequestEntrySchema = z.object({
  repayment_id: uuidField("The repayment id"),
  reason: z
    .string()
    .min(1, "A reason is required.")
    .max(500, "Keep the reason under 500 characters."),
});

export type AdjustmentRequestEntry = z.infer<typeof adjustmentRequestEntrySchema>;

export const writeOffRequestEntrySchema = z.object({
  loan_id: uuidField("The loan id"),
  reason: z
    .string()
    .min(1, "A reason is required.")
    .max(500, "Keep the reason under 500 characters."),
});

export type WriteOffRequestEntry = z.infer<typeof writeOffRequestEntrySchema>;

export const rejectReasonEntrySchema = z.object({
  reason: z
    .string()
    .min(1, "The rejection rationale is required (four-eyes practice).")
    .max(500, "Keep the reason under 500 characters."),
});

/** Amount entry for the recovery receipt — the typed decimal STRING
 * end-to-end (blocker (a)): validated for SHAPE only (no leading
 * zeros, at most two decimal places), never parsed into a number. The
 * server enforces the claim cap under the write-off row lock. */
export const receiptAmountEntrySchema = z.object({
  amount: z
    .string()
    .min(1, "Enter the amount received.")
    .regex(/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/, "Decimal amount, e.g. 5000 or 5000.50 — no leading zeros.")
    .refine((value) => value !== "0" && value !== "0.0" && value !== "0.00", {
      message: "The amount must be greater than zero.",
    }),
  channel: z.enum(["mpesa", "bank"], { message: "Select the cash channel." }),
});

export type ReceiptAmountEntry = z.infer<typeof receiptAmountEntrySchema>;
