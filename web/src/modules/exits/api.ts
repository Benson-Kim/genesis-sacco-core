/**
 * Member exit API layer (module 7 — the exit & settlement API)
 * over the GENERATED client.
 *
 * - Keyset pagination ONLY on the register read: opaque `cursor`
 *   echoed back verbatim; no offset or page parameters exist here
 *   (scalability). The single list filter the contract declares (status)
 *   is a SERVER query parameter — the client never filters locally.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the stability/rotation contract (concurrency safety). Void and settlement
 *   PIN the record version (the approved-snapshot rule): any drift
 *   since the operator's fresh read is a 409 with NOTHING posted —
 *   rendered via the explicit reload-and-re-enter flow, never
 *   replayed.
 * - NO money parameter exists in ANY request body (the exit fee comes exclusively from tenant configuration; every settlement figure is computed server-side under locks; `extra="forbid"` server-side). The only caller-chosen values in this module are
 *   the member, the optional reason, the vote and the CASH payout
 *   channel (the contract's require_cash_channel rejects
 *   accrual/internal).
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
  exitEligibilitySchema,
  exitSchema,
  exitStatementSchema,
  exitVoteResultSchema,
  settlementResultSchema,
  type ExitEligibility,
  type ExitRecord,
  type ExitStatement,
  type ExitStatus,
  type ExitVote,
  type ExitVoteResult,
  type SettlementResult,
} from "./schemas";

export const EXITS_PAGE_SIZE = 20;

const exitPageSchema = keysetPageSchema(exitSchema);

export interface ExitListFilters {
  /** The ONLY filter the list contract declares. */
  status: ExitStatus | "";
}

export const EMPTY_EXIT_FILTERS: ExitListFilters = { status: "" };

export async function fetchExitsPage(
  filters: ExitListFilters,
  cursor: string | null,
): Promise<KeysetPage<ExitRecord>> {
  const { data, error, response } = await api.GET("/member-exits", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: EXITS_PAGE_SIZE,
        status: filters.status === "" ? undefined : filters.status,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitPageSchema.parse(data);
}

/** Single exit record — the FRESH read (record class, staleTime 0)
 * behind every lifecycle write (members:view). */
export async function fetchExit(exitId: string): Promise<ExitRecord> {
  const { data, error, response } = await api.GET("/member-exits/{exit_id}", {
    params: { path: { exit_id: exitId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitSchema.parse(data);
}

/**
 * Create an exit request (members:edit): the server snapshots the
 * settlement (shares + deposits − loan balance − tenant-configured
 * fees) under the member row lock for committee approval. The body
 * carries the member and the optional reason ONLY — no money field
 * can even be sent.
 */
export async function createExitRequest(
  memberId: string,
  reason: string | null,
  idempotencyKey: string,
): Promise<ExitRecord> {
  const { data, error, response } = await api.POST("/member-exits", {
    body: { member_id: memberId, reason },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitSchema.parse(data);
}

/**
 * Cast a committee vote on an open exit (members:approve — the P9
 * quorum machinery; the server bans self-votes and enforces one vote
 * per voter regardless of what this client offers).
 */
export async function voteOnExit(
  exitId: string,
  vote: ExitVote,
  idempotencyKey: string,
): Promise<ExitVoteResult> {
  const { data, error, response } = await api.POST("/member-exits/{exit_id}/votes", {
    params: { path: { exit_id: exitId } },
    body: { vote },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitVoteResultSchema.parse(data);
}

/**
 * Void an open exit (members:approve): requested/approved → rejected.
 * The version pins the record state the operator acted on (1.4) —
 * drift is a 409, nothing changed.
 */
export async function voidExit(
  exitId: string,
  version: number,
  idempotencyKey: string,
): Promise<ExitRecord> {
  const { data, error, response } = await api.POST("/member-exits/{exit_id}/void", {
    params: { path: { exit_id: exitId } },
    body: { version },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitSchema.parse(data);
}

/**
 * Atomically post the APPROVED settlement (members:approve): the
 * server re-verifies every snapshot component under the full lock set
 * — any drift since approval is a 409 with NOTHING posted (the quote/approve/post rule). The body carries the pinned version and
 * the CASH payout channel ONLY.
 */
export async function postExitSettlement(
  exitId: string,
  version: number,
  channel: CashChannel,
  idempotencyKey: string,
): Promise<SettlementResult> {
  const { data, error, response } = await api.POST("/member-exits/{exit_id}/settlement", {
    params: { path: { exit_id: exitId } },
    body: { version, channel },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return settlementResultSchema.parse(data);
}

/** The canonical exit-statement JSON document (members:view) —
 * rendered VERBATIM; its CSV/PDF export ships via the reports module
 * (blocker (k)), never duplicated here. */
export async function fetchExitStatement(exitId: string): Promise<ExitStatement> {
  const { data, error, response } = await api.GET("/member-exits/{exit_id}/statement", {
    params: { path: { exit_id: exitId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitStatementSchema.parse(data);
}

/**
 * Advisory eligibility facts BEFORE a request is submitted
 * (`GET /member-exits/eligibility/{member_id}`, members:view). COUNTS/BOOLEANS ONLY: the contract carries no amount,
 * so nothing returned here can ever feed fmtKes. ADVISORY: the read is
 * lock-free — the server re-verdicts every blocker under locks at
 * request time and again at settlement; the binding verdict never
 * lives in this response. The member id rides the contract's PATH
 * parameter through the generated client; auth/tenant stay HEADERS.
 */
export async function fetchExitEligibility(memberId: string): Promise<ExitEligibility> {
  const { data, error, response } = await api.GET("/member-exits/eligibility/{member_id}", {
    params: { path: { member_id: memberId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitEligibilitySchema.parse(data);
}
