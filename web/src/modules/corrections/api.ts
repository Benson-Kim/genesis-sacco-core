/**
 * Corrections API layer (follow-on, — the corrections & write-off API plus the issue- recovery receipts) over the GENERATED client.
 * 
 * - REGISTERS (the human-authorized read-contract expansion that closed the gap): keyset LIST reads for adjustments (pending-first — the
 *   checker's job order) and write-offs (live-first — the committee's
 *   job order), both under corrections:view. By-id reads and every
 *   mutation are UNTOUCHED. All keyset reads follow scalability: opaque
 *   `cursor` echoed back verbatim; no offset/page parameters.",
 *
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the stability/rotation contract (concurrency safety). Reject and void PIN
 *   the record version in the wire body; approval and posting bodies
 *   are DELIBERATELY EMPTY (/: the checker approves the PERSISTED snapshot — the server re-verifies it under the full lock set, 409 on drift with NOTHING posted).
 * - NO money parameter exists in ANY request body except the recovery
 *   receipt's cash-received amount (the one caller-known figure —
 *   the cash physically in hand; sent as the typed decimal STRING,
 *   never a float). Fee amounts resolve from configuration,
 *   adjustment figures from the original transaction's append-only
 *   legs, write-off figures from the write-once 0025 snapshot
 *   (extra="forbid" server-side turns anything else into a 422).
 * - Ids travel as path parameters serialized by the generated client;
 *   bearer/tenant/idempotency secrets travel as HEADERS ONLY (gate
 *   1.6).
 * - MONEY (blocker (a)): every response figure is a server-computed
 *   decimal STRING asserted by the Zod boundary (numbers are
 *   REJECTED).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import type { CashChannel } from "@/modules/transactions/schemas";
import {
  adjustmentResultSchema,
  adjustmentSchema,
  feeResultSchema,
  recoveryReceiptResultSchema,
  recoveryReceiptsPageSchema,
  writeOffPostResultSchema,
  writeOffSchema,
  writeOffVoteResultSchema,
  type AdjustmentRecord,
  type AdjustmentResult,
  type FeeResult,
  type FeeType,
  type RecoveryReceiptResult,
  type RecoveryReceiptsPage,
  type WriteOffPostResult,
  type WriteOffRecord,
  type WriteOffVote,
  type WriteOffVoteResult,
} from "./schemas";

export const RECEIPTS_PAGE_SIZE = 20;
export const REGISTER_PAGE_SIZE = 20;

const adjustmentsPageSchema = keysetPageSchema(adjustmentSchema);
const writeOffsPageSchema = keysetPageSchema(writeOffSchema);

/**
 * The pending-adjustments checker register (corrections:view): keyset, PENDING FIRST then newest first — the
 * server's order, never re-sorted locally. The checker works from
 * this register instead of a hand-carried id.
 */
export async function fetchAdjustmentsPage(
  cursor: string | null,

): Promise<KeysetPage<AdjustmentRecord>> {
  const { data, error, response } = await api.GET("/corrections/repayment-adjustments", {
    params: { query: { cursor: cursor ?? undefined, limit: REGISTER_PAGE_SIZE } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return adjustmentsPageSchema.parse(data);
}

/**
 * The write-off committee register (corrections:view): keyset, LIVE (requested/approved) FIRST then newest
 * first — the server's order, never re-sorted locally. Vote/posting
 * semantics are untouched; this is a read.
 */
export async function fetchWriteOffsPage(
  cursor: string | null,
): Promise<KeysetPage<WriteOffRecord>> {
  const { data, error, response } = await api.GET("/corrections/write-offs", {
    params: { query: { cursor: cursor ?? undefined, limit: REGISTER_PAGE_SIZE } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffsPageSchema.parse(data);
}

/**
 * MAKER phase: create a PENDING adjustment bound to the
 * persisted approval snapshot (corrections:create). The body carries
 * the repayment and the reason ONLY — every figure derives server-side
 * from the original transaction's append-only legs.
 */
export async function requestAdjustment(
  repaymentId: string,
  reason: string,
  idempotencyKey: string,
): Promise<AdjustmentRecord> {
  const { data, error, response } = await api.POST("/corrections/repayment-adjustments", {
    body: { repayment_id: repaymentId, reason },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return adjustmentSchema.parse(data);
}

/** Single adjustment record — the FRESH read (record class, staleTime
 * 0) behind the checker writes (corrections:view). */
export async function fetchAdjustment(adjustmentId: string): Promise<AdjustmentRecord> {
  const { data, error, response } = await api.GET(
    "/corrections/repayment-adjustments/{adjustment_id}",
    { params: { path: { adjustment_id: adjustmentId } } },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return adjustmentSchema.parse(data);
}

/**
 * CHECKER phase (corrections:approve): approve the
 * PERSISTED snapshot. The body is DELIBERATELY EMPTY —
 * the server re-verifies every snapshot component under the full lock
 * set (409 on drift, posting nothing), then posts the storno reversal.
 * Maker ≠ checker is server-enforced AND a 0031 DB CHECK.
 */
export async function approveAdjustment(
  adjustmentId: string,
  idempotencyKey: string,
): Promise<AdjustmentResult> {
  const { data, error, response } = await api.POST(
    "/corrections/repayment-adjustments/{adjustment_id}/approval",
    {
      params: { path: { adjustment_id: adjustmentId } },
      body: {},
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return adjustmentResultSchema.parse(data);
}

/**
 * Reject a pending adjustment (checker decision, corrections:approve):
 * the version pins the record state the operator acted on (1.4) and
 * the rationale is REQUIRED (four-eyes practice puts the checker's reason on the record). Frees the one-live-adjustment slot.
 */
export async function rejectAdjustment(
  adjustmentId: string,
  version: number,
  reason: string,
  idempotencyKey: string,
): Promise<AdjustmentRecord> {
  const { data, error, response } = await api.POST(
    "/corrections/repayment-adjustments/{adjustment_id}/reject",
    {
      params: { path: { adjustment_id: adjustmentId } },
      body: { version, reason },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return adjustmentSchema.parse(data);
}

/**
 * Post a configured misc fee against a member (corrections:create,
 * FE- ref). The body names the member, the CODE-OWNED fee type and the
 * CASH channel ONLY — the amount resolves exclusively from
 * tenant configuration server-side (/).
 */
export async function postFee(
  memberId: string,
  feeType: FeeType,
  channel: CashChannel,
  idempotencyKey: string,
): Promise<FeeResult> {
  const { data, error, response } = await api.POST("/corrections/fees", {
    body: { member_id: memberId, fee_type: feeType, channel },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return feeResultSchema.parse(data);
}

/**
 * Persist the write-once write-off snapshot for committee approval
 * (corrections:create). The body carries the loan and the reason ONLY
 * — the snapshot figures are read under the loan row lock server-side
 * (v1.1 rules 1/3).
 */
export async function requestWriteOff(
  loanId: string,
  reason: string,
  idempotencyKey: string,
): Promise<WriteOffRecord> {
  const { data, error, response } = await api.POST("/corrections/write-offs", {
    body: { loan_id: loanId, reason },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffSchema.parse(data);
}

/** Single write-off record — the FRESH read (record class, staleTime
 * 0) behind every lifecycle write (corrections:view). */
export async function fetchWriteOff(writeOffId: string): Promise<WriteOffRecord> {
  const { data, error, response } = await api.GET("/corrections/write-offs/{write_off_id}", {
    params: { path: { write_off_id: writeOffId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffSchema.parse(data);
}

/**
 * Cast a committee vote on a requested write-off (corrections:approve
 * — the P9 quorum machinery; the server bans requester self-votes and
 * enforces one vote per voter regardless of what this client offers).
 */
export async function voteOnWriteOff(
  writeOffId: string,
  vote: WriteOffVote,
  idempotencyKey: string,
): Promise<WriteOffVoteResult> {
  const { data, error, response } = await api.POST(
    "/corrections/write-offs/{write_off_id}/votes",
    {
      params: { path: { write_off_id: writeOffId } },
      body: { vote },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffVoteResultSchema.parse(data);
}

/**
 * Void a drifted or withdrawn write-off (corrections:approve): frees
 * the per-loan slot. The version pins the record state the operator
 * acted on (1.4) — drift is a 409, nothing changed.
 */
export async function voidWriteOff(
  writeOffId: string,
  version: number,
  idempotencyKey: string,
): Promise<WriteOffRecord> {
  const { data, error, response } = await api.POST(
    "/corrections/write-offs/{write_off_id}/void",
    {
      params: { path: { write_off_id: writeOffId } },
      body: { version },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffSchema.parse(data);
}

/**
 * Execute an APPROVED write-off (corrections:approve): the WO-
 * provisioning posting. The body is DELIBERATELY EMPTY —
 * every figure comes from the persisted write-once snapshot, re-
 * verified component-by-component under the loan row lock (409 on
 * drift, posting nothing).
 */
export async function postWriteOff(
  writeOffId: string,
  idempotencyKey: string,
): Promise<WriteOffPostResult> {
  const { data, error, response } = await api.POST(
    "/corrections/write-offs/{write_off_id}/posting",
    {
      params: { path: { write_off_id: writeOffId } },
      body: {},
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return writeOffPostResultSchema.parse(data);
}

/**
 * Record a bad-debt recovery receipt against a POSTED write-off
 * (corrections:create): RC- posting + append-only receipt
 * row — never a resurrection of the loan. The amount is the cash
 * physically received, sent as the typed decimal STRING (blocker (a));
 * a receipt exceeding the outstanding claim is a 409 (the claim
 * figures are server-resolved under the write-off row lock).
 */
export async function recordRecoveryReceipt(
  writeOffId: string,
  amount: string,
  channel: CashChannel,
  idempotencyKey: string,
): Promise<RecoveryReceiptResult> {
  const { data, error, response } = await api.POST(
    "/corrections/write-offs/{write_off_id}/recoveries",
    {
      params: { path: { write_off_id: writeOffId } },
      body: { amount, channel },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return recoveryReceiptResultSchema.parse(data);
}

/**
 * Receipts recorded against one write-off claim (corrections:view; keyset, oldest first — scalability) with the server-reconstructed
 * recovered/outstanding position.
 */
export async function fetchRecoveryReceiptsPage(
  writeOffId: string,
  cursor: string | null,
): Promise<RecoveryReceiptsPage> {
  const { data, error, response } = await api.GET(
    "/corrections/write-offs/{write_off_id}/recoveries",
    {
      params: {
        path: { write_off_id: writeOffId },
        query: { cursor: cursor ?? undefined, limit: RECEIPTS_PAGE_SIZE },
      },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return recoveryReceiptsPageSchema.parse(data);
}
