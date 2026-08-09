import { z } from "zod";
import { isoTimestampSchema, moneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the transactions module (the
 * P11 API). Shapes mirror the generated client types
 * (components["schemas"]["TransactionOut"], AccountTxnOut,
 * InterestRunOut) — the drift-checked OpenAPI snapshot remains the
 * contract; these schemas only assert it at runtime.
 *
 * MONEY RULE (no client-side money math): every monetary field (amount,
 * balance_after, total_interest) is a decimal STRING end-to-end,
 * rendered via fmtKes or verbatim and never coerced or combined
 * arithmetically. Running balances and ledger totals are SERVER facts;
 * the prototype's client-side totals row is deliberately NOT
 * reproduced. Debits and credits are never netted or summed locally.
 */

/** Mirrors the backend TxnType enum (domain/ledger.py). An unknown
 * value is a contract violation and is REJECTED, never rendered. */
export const TXN_TYPES = [
  "deposit",
  "withdrawal",
  "share_topup",
  "loan_disbursement",
  "loan_repayment",
  "interest_posting",
  "exit_settlement",
  "dividend_posting",
  "share_transfer_out",
  "share_transfer_in",
  "fee",
  "loan_write_off",
  "loan_recovery",
] as const;
export const txnTypeSchema = z.enum(TXN_TYPES);
export type TxnType = z.infer<typeof txnTypeSchema>;

export const TXN_TYPE_LABELS: Record<TxnType, string> = {
  deposit: "Deposit",
  withdrawal: "Withdrawal",
  share_topup: "Share top-up",
  loan_disbursement: "Disbursement",
  loan_repayment: "Loan repayment",
  interest_posting: "Interest",
  exit_settlement: "Exit settlement",
  dividend_posting: "Dividend",
  share_transfer_out: "Share transfer out",
  share_transfer_in: "Share transfer in",
  fee: "Fee",
  loan_write_off: "Write-off",
  loan_recovery: "Recovery",
};

/** Mirrors the backend Channel enum. All four values appear on ledger
 * ROWS (accrual and internal postings are made by server jobs); only
 * the CASH channels are offered on writes (require_cash_channel). */
export const CHANNELS = ["mpesa", "bank", "accrual", "internal"] as const;
export const channelSchema = z.enum(CHANNELS);
export type Channel = z.infer<typeof channelSchema>;

export const CHANNEL_LABELS: Record<Channel, string> = {
  mpesa: "M-Pesa",
  bank: "Bank",
  accrual: "Accrual",
  internal: "Internal",
};

/** Channels on which money physically moves — the ONLY ones the P11
 * write contract accepts (backend require_cash_channel rejects
 * accrual and internal with a 400); the UI never offers what the API
 * forbids (least disclosure). */
export const CASH_CHANNELS = ["mpesa", "bank"] as const;
export const cashChannelSchema = z.enum(CASH_CHANNELS);
export type CashChannel = z.infer<typeof cashChannelSchema>;

/** Mirrors the backend Side enum — the ledger leg this row reports.
 * The direction decides WHICH column renders the amount; the client
 * never nets a debit against a credit (blocker (a)). */
export const SIDES = ["debit", "credit"] as const;
export const sideSchema = z.enum(SIDES);
export type Side = z.infer<typeof sideSchema>;

export const SIDE_LABELS: Record<Side, string> = {
  debit: "Debit",
  credit: "Credit",
};

/** One ledger row (TransactionOut). The list model carries NO running
 * balance — balances are SERVER facts served on write responses
 * (AccountTxnOut.balance_after) and statements, never reconstructed
 * from list pages. */
export const transactionSchema = z.object({
  id: z.string(),
  txn_ref: z.string(),
  member_id: z.string().nullable(),
  type: txnTypeSchema,
  /** CANONICAL server money shape:
   * `transactions.amount` is numeric(18,2) CHECK (amount > 0)
   * (migration 0001) serialised via `str(Decimal)` (_txn_out,
   * api/transactions.py) — a '-' or a garbage shape is a contract
   * violation, REJECTED before it can reach fmtKes. */
  amount: moneySchema,
  channel: channelSchema,
  direction: sideSchema,
  /** `datetime.isoformat()` shape — feeds fmtDateTime on the register
   * (the strict-date lesson): garbage is REJECTED, never "Invalid Date". */
  occurred_at: isoTimestampSchema,
  is_reversal: z.boolean(),
  /** Posting-actor attribution (migration 0036 — the
   * attribution follow-up): the staff principal who posted this row, as the
   * BARE user UUID only. Least disclosure: never a name or
   * email — resolving the id stays behind access_control:view
   * server-side, and this client NEVER fetches a directory record for
   * it. NULL is an honest "unattributed" (system/job postings —
   * interest, dormancy — and pre-0036 rows without unambiguous audit
   * history; FM-B: an actor is never invented). */
  created_by: z.string().nullable(),
  /** External receipt reference (migration 0043) —
   * NULLABLE, never optional: the operator-entered M-Pesa code / bank
   * slip ref on external-channel teller postings; null is the honest
   * state for system postings and pre-0043 history. Renders as inert
   * React text only. */
  external_ref: z.string().nullable(),
});

export type Transaction = z.infer<typeof transactionSchema>;

/** One DR or CR leg of a posting (LedgerLegOut — the
 * expand-only legs drill-down under transactions:view). Verbatim
 * ledger_entries facts, one row per leg, NOTHING derived:
 * - account: the chart-of-accounts KEY (backend Account enum). An
 *   OPEN vocabulary at this boundary by design — the chart grows with
 *   backend features (new liability and
 *   added recovery accounts) and a stale client pin would REJECT
 *   legitimate ledger facts; the key renders exclusively as inert
 *   React text and never drives a parser or a branch.
 * - side: the CLOSED two-value vocabulary (shared sideSchema) — an
 *   unknown side is a contract violation and is REJECTED.
 * - amount: `ledger_entries.amount` is numeric(18,2) CHECK (amount >
 *   0) (migration 0001) serialised via str(Decimal) — the shared
 *   moneySchema shape; a sign or a garbage shape is REJECTED before
 *   it can reach fmtKes. Legs render VERBATIM and are never summed or
 *   netted (no client-side money math): the 0004/0014 DB trigger proved balance
 *   at commit time — no client-side balancing total exists. */
export const ledgerLegSchema = z.object({
  account: z.string(),
  side: sideSchema,
  amount: moneySchema,
});

export type LedgerLeg = z.infer<typeof ledgerLegSchema>;

/** TransactionLegsResponse: items ONLY — the contract is unpaginated
 * BY CONSTRUCTION (the widest posting builder writes 7 legs) and
 * carries NO derived key (neither a sum nor a net figure); anything
 * wider is stripped, never rendered. */
export const transactionLegsSchema = z.object({
  items: z.array(ledgerLegSchema),
});

/** Teller-write result (AccountTxnOut): the posted amount and the
 * SERVER-computed balance after, verbatim. Extra keys are STRIPPED at
 * this boundary. */
export const accountTxnSchema = z.object({
  txn_id: z.string(),
  txn_ref: z.string(),
  /** to_cents results (application/transactions.py), both fmtKes-fed:
   * canonical two-place shape; `deposit_accounts.balance` carries
   * CHECK (balance >= 0) (migration 0001) so a '-' is a contract
   * violation on either field. */
  amount: moneySchema,
  balance_after: moneySchema,
});

export type AccountTxn = z.infer<typeof accountTxnSchema>;

/** Deposit-interest run result (InterestRunOut) — every figure is the
 * server's (period resolved server-side in strict quarter order; the
 * rate comes exclusively from tenant configuration). */
export const interestRunSchema = z.object({
  /** Quarter bounds are DATE isoformat ("YYYY-MM-DD",
   * api/transactions.py period.start.isoformat()) rendered VERBATIM in
   * the dialog — deliberately NOT isoTimestampSchema, which would
   * reject every legitimate response. */
  period_start: z.string(),
  period_end: z.string(),
  /** Tenant-configured rate string (not money, never fmtKes-fed) —
   * scale is the stored configuration's, so no shape is asserted. */
  annual_rate_pct: z.string(),
  scanned: z.number().int(),
  posted: z.number().int(),
  skipped_existing: z.number().int(),
  rate_mismatches: z.number().int(),
  /** ZERO ("0.00") plus to_cents postings (deposit_interest.py) — the
   * canonical two-place shape even for an all-skipped run; fmtKes-fed
   *. */
  total_interest: moneySchema,
  batches: z.number().int(),
});

export type InterestRun = z.infer<typeof interestRunSchema>;

/** The three P11 teller writes this batch owns. Loan repayments ship
 * with the Loan book batch; fees belong to the corrections RBAC
 * module — neither is duplicated here. */
export const TXN_WRITE_KINDS = ["deposit", "withdrawal", "share_topup"] as const;
export const txnWriteKindSchema = z.enum(TXN_WRITE_KINDS);
export type TxnWriteKind = z.infer<typeof txnWriteKindSchema>;

export const TXN_WRITE_KIND_LABELS: Record<TxnWriteKind, string> = {
  deposit: "Deposit",
  withdrawal: "Withdrawal",
  share_topup: "Share top-up",
};

/** Pure STRING decimal shape (2dp max), rejecting leading zeros —
 * "007.10" is not a ledger amount. No numeric
 * coercion happens anywhere on this value. */
const DECIMAL_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;
const ALL_ZERO_RE = /^0(?:\.0{1,2})?$/;

/** External-reference shapes  — a courtesy
 * mirror of the server's PER-CHANNEL require_external_ref shapes
 * (M-Pesa confirmation code: exactly 10 letters/digits; bank slip
 * ref: 2–40 chars, alphanumeric with space / - separators,
 * alphanumeric at both edges). The server trims, normalizes
 * UPPERCASE and enforces both the shape and the per-tenant
 * per-channel dedupe regardless — least disclosure. */
export const MPESA_REF_RE = /^[A-Za-z0-9]{10}$/;
export const BANK_REF_RE = /^[A-Za-z0-9][A-Za-z0-9 /-]{0,38}[A-Za-z0-9]$/;

/**
 * Client-side pre-validation of the post-transaction form (the server
 * re-validates and is the enforcer — least disclosure). The amount is
 * validated as a decimal STRING shape and sent as a string: it is
 * never parsed into a float.
 */
export const moneyEntrySchema = z.object({
  kind: txnWriteKindSchema,
  member_id: z.string().min(1, "Select a member."),
  amount: z
    .string()
    .regex(
      DECIMAL_RE,
      "Enter a positive amount (up to 2 decimal places, no leading zeros).",
    )
    .refine((value) => !ALL_ZERO_RE.test(value), {
      message: "Enter an amount greater than zero.",
    }),
  channel: z.enum(CASH_CHANNELS, {
    errorMap: () => ({ message: "Select a channel." }),
  }),
  /** REQUIRED on every teller posting: both offered channels are
   * EXTERNAL money movements; the shape is validated
   * per channel in the superRefine below (review R3). */
  external_ref: z.string(),
}).superRefine((entry, ctx) => {
  const valid =
    entry.channel === "mpesa"
      ? MPESA_REF_RE.test(entry.external_ref)
      : BANK_REF_RE.test(entry.external_ref);
  if (!valid) {
    ctx.addIssue({
      code: "custom",
      path: ["external_ref"],
      message:
        entry.channel === "mpesa"
          ? "Enter the 10-character M-Pesa confirmation code (letters/digits)."
          : "Enter the bank reference (2–40 letters/digits).",
    });
  }
});

export type MoneyEntryInput = z.infer<typeof moneyEntrySchema>;

/** ISO date (YYYY-MM-DD) or empty — the register's date filters. */
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
