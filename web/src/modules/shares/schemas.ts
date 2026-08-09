import { z } from "zod";
import { moneySchema } from "@/lib/schemas";

/**
 * Zod-validated boundary for the share-transfers console (the human-authorized remediation reworking the ledger-(e) console). Shapes mirror the
 * generated client types (components["schemas"]["ShareTransfer*"]) —
 * the drift-checked OpenAPI snapshot remains the contract; these
 * schemas only assert it at runtime.
 *
 * THE CONTRACT (two-phase maker-checker + register):
 * - `POST /members/{member_id}/share-transfers` — MAKER phase
 *   (members:approve): creates a PENDING workflow record bound to the
 *   persisted approval snapshot. NO money moves.
 * - `POST /share-transfers/{id}/approval` — CHECKER phase
 *   (members:approve, a DIFFERENT principal; assurance excluded;
 *   maker ≠ checker is ALSO a 0040 DB CHECK): the server re-verifies
 *   the snapshot under the full lock set (409 on drift posting
 *   NOTHING), then posts BOTH ledger legs atomically and notifies
 *   BOTH members via the outbox.
 * - `POST /share-transfers/{id}/rejection` — version-pinned checker
 *   rejection with the MANDATORY rationale.
 * - `GET /share-transfers` (+ by-id) — the ledger-(m) history
 *   register (members:view, the house read-split): keyset, PENDING
 *   FIRST then newest first (the server's order, never re-sorted).
 *
 * MONEY: the request amount is the operation's
 * SUBJECT, typed as a decimal STRING end-to-end and never parsed into
 * a number. Every response figure is a server-computed decimal string
 * asserted by moneySchema and rendered VERBATIM — balances belong to
 * two different members and are never summed, netted or reconciled
 * client-side.
 *
 * LEAST DISCLOSURE: register rows carry bare UUIDs (the / short-id convention) and NO balance snapshot — the audit rows hold
 * the exact figures server-side.
 */

export const TRANSFER_STATUSES = ["pending", "posted", "rejected"] as const;
export type TransferStatus = (typeof TRANSFER_STATUSES)[number];

export const TRANSFER_STATUS_LABELS: Record<TransferStatus, string> = {
  pending: "Pending approval",
  posted: "Posted",
  rejected: "Rejected",
};

export function isTerminalTransferStatus(status: TransferStatus): boolean {
  return status !== "pending";
}

/** ShareTransferRecordOut — the workflow record (register rows, the
 * request/rejection responses and the by-id read). Every key REQUIRED
 * (key-exact: a missing key is contract drift and refuses to parse;
 * extra keys are stripped). Maker/checker attribution and the ledger
 * transaction ids are NULLABLE-never-optional server truth: a null is
 * honest history (pre-0040 rows carry no approver; a pending row has
 * posted nothing), never an invented value. */
export const shareTransferRecordSchema = z.object({
  id: z.string(),
  from_member_id: z.string(),
  to_member_id: z.string(),
  amount: moneySchema,
  status: z.enum(TRANSFER_STATUSES),
  out_transaction_id: z.string().nullable(),
  in_transaction_id: z.string().nullable(),
  created_by: z.string().nullable(),
  approved_by: z.string().nullable(),
  decided_at: z.string().nullable(),
  version: z.number().int().min(1),
  created_at: z.string(),
});

export type ShareTransferRecord = z.infer<typeof shareTransferRecordSchema>;

/** ShareTransferOut — the APPROVAL result: the money actually moved
 * (phase 2). Every key REQUIRED; no nullable leg exists. */
export const shareTransferResultSchema = z.object({
  transfer_id: z.string(),
  /** The ST-OUT / ST-IN double-entry pair refs — evidence of BOTH
   * legs of the posting, rendered verbatim. */
  out_txn_ref: z.string(),
  in_txn_ref: z.string(),
  amount: moneySchema,
  from_balance_after: moneySchema,
  to_balance_after: moneySchema,
});

export type ShareTransferResult = z.infer<typeof shareTransferResultSchema>;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Client-side pre-validation of the maker's request form (the server re-validates and is the enforcer — least disclosure: 2dp bound at the contract, balance check under the full lock set, active-member and distinct-members checks server-side; self-transfer is ALSO a 0020 DB CHECK).
 */
export const transferEntrySchema = z
  .object({
    from_member_id: z
      .string()
      .min(1, "The transferring member id is required.")
      .regex(UUID_RE, "Enter the full member UUID (8-4-4-4-12 hex)."),
    to_member_id: z
      .string()
      .min(1, "The receiving member id is required.")
      .regex(UUID_RE, "Enter the full member UUID (8-4-4-4-12 hex)."),
    amount: z
      .string()
      .min(1, "Enter the amount to transfer.")
      .regex(
        /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/,
        "Decimal amount, e.g. 5000 or 5000.50 — no leading zeros.",
      )
      .refine((value) => value !== "0" && value !== "0.0" && value !== "0.00", {
        message: "The amount must be greater than zero.",
      }),
  })
  .superRefine((entry, ctx) => {
    // The server refuses a self-transfer regardless; refusing it here
    // saves the operator a pointless armed confirmation.
    if (
      UUID_RE.test(entry.from_member_id) &&
      entry.from_member_id.toLowerCase() === entry.to_member_id.toLowerCase()
    ) {
      ctx.addIssue({
        code: "custom",
        path: ["to_member_id"],
        message: "Shares cannot be transferred to the same member.",
      });
    }
  });

export type TransferEntry = z.infer<typeof transferEntrySchema>;

/** The checker's rejection rationale — REQUIRED (F2; the server enforces min_length=1/max 500 at the contract regardless). */
export const rejectReasonEntrySchema = z.object({
  reason: z
    .string()
    .min(1, "The rejection rationale is required (four-eyes practice).")
    .max(500, "Keep the reason under 500 characters."),
});
