import { z } from "zod";
import { aggregateMoneySchema, isoTimestampSchema, moneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the loan-book module (module 4 — API). Shapes mirror the generated client types
 * (components["schemas"]["LoanOut"] etc.) — the drift-checked OpenAPI
 * snapshot remains the contract; these schemas only assert it at
 * runtime.
 *
 * MONEY RULE: every monetary field (principal,
 * balance, penalty_due, schedule/quote figures, portfolio aggregates)
 * is a decimal STRING end-to-end, rendered via fmtKes or verbatim and
 * never coerced or combined arithmetically. Outstanding balances,
 * arrears classification, provisioning, schedules, settlement totals
 * and repayment allocations are SERVER facts.
 */

/** Mirrors the backend LoanStatus enum. An unknown value is a contract
 * violation and is REJECTED, never silently rendered. */
export const LOAN_STATUSES = ["active", "closed", "written_off"] as const;
export const loanStatusSchema = z.enum(LOAN_STATUSES);
export type LoanStatus = z.infer<typeof loanStatusSchema>;

export const LOAN_STATUS_LABELS: Record<LoanStatus, string> = {
  active: "Active",
  closed: "Closed",
  written_off: "Written off",
};

/** Mirrors the backend LoanClass enum (arrears classification — a
 * SERVER-computed fact from the nightly arrears job; the prototype's
 * client-side classify(days) is deliberately NOT reproduced). */
export const LOAN_CLASSES = [
  "normal",
  "watch",
  "substandard",
  "doubtful",
  "loss",
] as const;
export const loanClassSchema = z.enum(LOAN_CLASSES);
export type LoanClass = z.infer<typeof loanClassSchema>;

export const LOAN_CLASS_LABELS: Record<LoanClass, string> = {
  normal: "Normal",
  watch: "Watch",
  substandard: "Substandard",
  doubtful: "Doubtful",
  loss: "Loss",
};

export const loanSchema = z.object({
  id: z.string(),
  application_id: z.string(),
  member_id: z.string(),
  product_id: z.string(),
  /** CANONICAL server money shapes (retrofit), all
   * fmtKes-fed: `loans.principal` CHECK (principal > 0),
   * `loans.balance` CHECK (balance >= 0) (migration 0001) and
   * `loans.penalty_due` CHECK (penalty_due >= 0) (migration 0007) are
   * numeric(18,2) serialised via str(Decimal) (_loan_out,
   * api/loan_book.py) — a '-' or garbage shape is a contract
   * violation, REJECTED before it can reach fmtKes. */
  principal: moneySchema,
  balance: moneySchema,
  /** Rate string (numeric(5,2)) — a percentage, never fmtKes-fed; the
   * shape is deliberately NOT money-asserted. */
  rate_pct: z.string(),
  term_months: z.number().int(),
  status: loanStatusSchema,
  classification: loanClassSchema,
  days_past_due: z.number().int(),
  /** Decimal string — the tenant's provisioning rate for the class; the
   * derived provision KES figure is NOT computed client-side (the
   * portfolio summary carries the server-computed total). */
  provision_pct: z.string(),
  penalty_due: moneySchema,
  /** datetime.isoformat() shapes — both feed fmtDateTime on the drawer
   * (an earlier review lesson): garbage is REJECTED, never "Invalid Date". */
  disbursed_at: isoTimestampSchema.nullable(),
  closed_at: isoTimestampSchema.nullable(),
  version: z.number().int(),
});

export type Loan = z.infer<typeof loanSchema>;

/** One repayment-schedule installment (ScheduleRowOut) — every figure a
 * server-computed decimal string, rendered verbatim. */
export const scheduleRowSchema = z.object({
  installment_no: z.number().int(),
  /** DATE isoformat ("YYYY-MM-DD", _schedule_out row.due_date) rendered
   * VERBATIM — deliberately NOT isoTimestampSchema, which would reject
   * every legitimate response (sweep note). */
  due_date: z.string(),
  /** loan_schedules columns, each CHECK (>= 0) numeric(18,2)
   * (migration 0001) via str(Decimal) — fmtKes-fed (retrofit). */
  principal_due: moneySchema,
  interest_due: moneySchema,
  total_due: moneySchema,
  paid_amount: moneySchema,
});

export const scheduleSchema = z.array(scheduleRowSchema);
export type ScheduleRow = z.infer<typeof scheduleRowSchema>;

/** Early settlement quote (SettlementQuoteOut) — as-of stamped server
 * facts; the total is the SERVER's sum, never recomputed here. */
export const settlementQuoteSchema = z.object({
  /** DATE isoformat (quote_date.isoformat(), api/loan_book.py) rendered
   * VERBATIM — deliberately NOT isoTimestampSchema (sweep note). */
  as_of: z.string(),
  /** Every bucket is to_cents-quantised by domain settlement_quote()
   * (which also REFUSES negative buckets) and the total is the
   * to_cents property sum — canonical non-negative two-place shapes,
   * all fmtKes-fed (retrofit). */
  principal_balance: moneySchema,
  interest_due: moneySchema,
  penalties_due: moneySchema,
  total: moneySchema,
});

export type SettlementQuote = z.infer<typeof settlementQuoteSchema>;

/** Disbursement result (DisbursementOut): the created loan id, the
 * ledger transaction reference and the SERVER-computed schedule. Extra
 * keys are STRIPPED at this boundary. */
export const disbursementSchema = z.object({
  loan_id: z.string(),
  txn_id: z.string(),
  txn_ref: z.string(),
  schedule: scheduleSchema,
});

export type Disbursement = z.infer<typeof disbursementSchema>;

/** Repayment result (RepaymentOut): the allocation (penalties, then
 * interest, then principal — allocated SERVER-side), the balance after
 * and the loan status after. All decimal strings, rendered verbatim. */
export const repaymentResultSchema = z.object({
  txn_id: z.string(),
  txn_ref: z.string(),
  /** Allocation buckets (domain allocate_repayment: min and to_cents
   * over non-negative dues) and the post-write balances (to_cents;
   * the loans balance and penalty_due CHECKs >= 0) — canonical
   * non-negative shapes, all fmtKes-fed (retrofit). */
  penalties: moneySchema,
  interest: moneySchema,
  principal: moneySchema,
  balance_after: moneySchema,
  penalty_due_after: moneySchema,
  status: loanStatusSchema,
});

export type RepaymentResult = z.infer<typeof repaymentResultSchema>;

/** Portfolio aggregates (PortfolioSummaryOut) — the NPL, PAR-30 and
 * provisioning figures are server-computed; the prototype's client-side
 * reductions over loan rows are deliberately NOT reproduced. */
export const classificationSliceSchema = z.object({
  classification: loanClassSchema,
  count: z.number().int(),
  /** SQL aggregates (COALESCE(SUM(...), 0), application/loans.py
   * portfolio_summary): an empty aggregate legitimately serialises as
   * the bare "0" — the aggregate shape is the honest boundary
   * (retrofit); fmtKes-fed. */
  balance: aggregateMoneySchema,
  provisions: aggregateMoneySchema,
});

export const portfolioSummarySchema = z.object({
  active_loans: z.number().int(),
  /** COALESCE(SUM(...), 0) aggregates via str(Decimal) — bare "0" on
   * an empty book, two-place otherwise; every one fmtKes-fed
   * (retrofit). */
  outstanding_balance: aggregateMoneySchema,
  npl_balance: aggregateMoneySchema,
  /** Ratio percentages (to_cents ratios, ZERO when the book is empty)
   * — rendered as "%" text, never fmtKes-fed: deliberately left
   * unasserted (sweep note). */
  npl_ratio_pct: z.string(),
  par30_balance: aggregateMoneySchema,
  par30_ratio_pct: z.string(),
  provisions: aggregateMoneySchema,
  penalties_due: aggregateMoneySchema,
  by_classification: z.array(classificationSliceSchema),
});

export type PortfolioSummary = z.infer<typeof portfolioSummarySchema>;

/** Money-movement channels the contract accepts for disbursement
 * and repayments (require_cash_channel: mpesa | bank — accrual/internal
 * are engine-owned and the UI never offers them). */
export const CASH_CHANNELS = ["mpesa", "bank"] as const;
export const cashChannelSchema = z.enum(CASH_CHANNELS);
export type CashChannel = z.infer<typeof cashChannelSchema>;

export const CHANNEL_LABELS: Record<CashChannel, string> = {
  mpesa: "M-Pesa",
  bank: "Bank",
};

/** Leading zeros rejected (W59-5, the F6 class): "007.10" is not a
 * well-formed decimal string and the typed-confirmation banner must
 * never render "KES 007.10". Pure string shape — no numeric coercion. */
const DECIMAL_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;
const ALL_ZERO_RE = /^0(?:\.0{1,2})?$/;

/**
 * Client-side pre-validation of the repayment form (the server re-validates and is the enforcer — least disclosure). The amount is validated
 * as a decimal STRING shape (2dp, non-zero — pure string inspection,
 * no numeric coercion) and sent as a string: it is never parsed into a
 * float.
 */
export const repaymentCreateSchema = z.object({
  amount: z
    .string()
    .regex(DECIMAL_RE, "Enter a positive amount (up to 2 decimal places).")
    .refine((value) => !ALL_ZERO_RE.test(value), {
      message: "Enter an amount greater than zero.",
    }),
  channel: z.enum(CASH_CHANNELS, {
    errorMap: () => ({ message: "Select a channel." }),
  }),
});

export type RepaymentCreateInput = z.infer<typeof repaymentCreateSchema>;
