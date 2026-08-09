/**
 * Share-transfers API layer (the human-authorized maker-checker rework) over the GENERATED
 * client.
 *
 * WHAT THE CONTRACT EXPOSES (read the router, consume exactly that):
 * - `POST /members/{member_id}/share-transfers` (members:approve) —
 *   MAKER phase: creates the PENDING workflow record bound to the
 *   persisted approval snapshot; NO money moves here.
 * - `GET /share-transfers` (members:view — the house read-split) —
 *   the ledger-(m) keyset register, PENDING FIRST then newest first
 *   (the checker's job order; scalability: opaque cursor echoed back verbatim, no offset/page parameters, hard max 100 server-side).
 * - `GET /share-transfers/{transfer_id}` (members:view) — the FRESH
 *   record read behind every checker write.
 * - `POST /share-transfers/{transfer_id}/approval` (members:approve,
 *   DIFFERENT principal) — the body is DELIBERATELY EMPTY (v1.1 rule
 *   3): the checker approves the PERSISTED snapshot; the server
 *   re-verifies it under the full lock set (409 on drift, posting
 *   NOTHING), then posts BOTH ST- legs and notifies BOTH members.
 * - `POST /share-transfers/{transfer_id}/rejection` — version-pinned
 *   with the MANDATORY rationale.
 *
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the web/README.md MATERIAL rules (concurrency safety).
 * - MONEY (blocker (a)): the request amount is the operation's
 *   SUBJECT — the typed decimal STRING end-to-end, never a float; NO
 *   other money parameter exists on any body (extra="forbid"
 *   server-side). Every response figure is a server-computed decimal
 *   string asserted by the Zod boundary.
 * - Ids travel as path/body parameters serialized by the generated
 *   client; bearer/tenant/idempotency secrets travel as HEADERS ONLY
 *   (least disclosure).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  shareTransferRecordSchema,
  shareTransferResultSchema,
  type ShareTransferRecord,
  type ShareTransferResult,
} from "./schemas";

export const REGISTER_PAGE_SIZE = 20;

const transfersPageSchema = keysetPageSchema(shareTransferRecordSchema);

/**
 * MAKER phase ((l), members:approve): create a PENDING
 * transfer bound to the persisted approval snapshot. The body carries
 * the transferee and the subject amount ONLY; the server enforces
 * active-member, distinct-members and sufficient-balance under the
 * full lock set — and NOTHING posts until a DISTINCT checker
 * approves.
 */
export async function requestShareTransfer(
  fromMemberId: string,
  toMemberId: string,
  amount: string,
  idempotencyKey: string,
): Promise<ShareTransferRecord> {
  const { data, error, response } = await api.POST("/members/{member_id}/share-transfers", {
    params: { path: { member_id: fromMemberId } },
    body: { to_member_id: toMemberId, amount },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return shareTransferRecordSchema.parse(data);
}

/**
 * The share-transfer history register (members:view): keyset, PENDING FIRST then newest first — the server's order,
 * never re-sorted locally. The checker works from this register; the
 * auditor walks the full history behind the pending band.
 */
export async function fetchShareTransfersPage(
  cursor: string | null,
): Promise<KeysetPage<ShareTransferRecord>> {
  const { data, error, response } = await api.GET("/share-transfers", {
    params: { query: { cursor: cursor ?? undefined, limit: REGISTER_PAGE_SIZE } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return transfersPageSchema.parse(data);
}

/** Single transfer record — the FRESH read (record class, staleTime
 * 0) behind the checker writes (members:view). */
export async function fetchShareTransfer(transferId: string): Promise<ShareTransferRecord> {
  const { data, error, response } = await api.GET("/share-transfers/{transfer_id}", {
    params: { path: { transfer_id: transferId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return shareTransferRecordSchema.parse(data);
}

/**
 * CHECKER phase ((l), members:approve): approve the
 * PERSISTED snapshot. The body is DELIBERATELY EMPTY —
 * the server re-verifies every snapshot component under the full lock
 * set (409 on drift, posting nothing), then posts BOTH ST- legs,
 * updates both balances and notifies BOTH members atomically. Maker ≠
 * checker is server-enforced AND a 0040 DB CHECK.
 */
export async function approveShareTransfer(
  transferId: string,
  idempotencyKey: string,
): Promise<ShareTransferResult> {
  const { data, error, response } = await api.POST("/share-transfers/{transfer_id}/approval", {
    params: { path: { transfer_id: transferId } },
    body: {},
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return shareTransferResultSchema.parse(data);
}

/**
 * Reject a pending transfer (checker decision, members:approve): the
 * version pins the record state the operator acted on (1.4) and the
 * rationale is REQUIRED (four-eyes practice puts the checker's reason on the record). No money moves; the rejected row
 * is terminal write-once workflow history.
 */
export async function rejectShareTransfer(
  transferId: string,
  version: number,
  reason: string,
  idempotencyKey: string,
): Promise<ShareTransferRecord> {
  const { data, error, response } = await api.POST("/share-transfers/{transfer_id}/rejection", {
    params: { path: { transfer_id: transferId } },
    body: { version, reason },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return shareTransferRecordSchema.parse(data);
}
