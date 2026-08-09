/**
 * Dividends lifecycle API layer (the declare/approve/distribute contract) over the GENERATED client.
 *
 * - Keyset pagination ONLY on the register read: opaque `cursor`
 *   echoed back verbatim; no offset or page parameters exist here
 *   (scalability). The list contract declares NO filters — the
 *   client sends none and filters nothing locally.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the web/README.md MATERIAL rule (concurrency safety). Void PINS the record
 *   version (the approved-snapshot rule): any drift since the
 *   operator's fresh read is a 409 with NOTHING changed — rendered via
 *   the explicit reload-and-re-enter flow, never replayed.
 * - NO money parameter exists in ANY request body: the
 *   rates and the financial-year period come exclusively from tenant
 *   configuration, every total from the persisted approval snapshot;
 *   `extra="forbid"` server-side turns a caller-supplied rate, period
 *   or total into a 422. The ONLY caller-chosen value in this module
 *   is the committee vote. Declare and distribute carry NO
 *   operator-chosen value either: the contract's lone knob,
 *   batch_size, is a server tuning parameter — the generated type
 *   requires the key, so this console sends the SERVER's own
 *   documented default verbatim (SERVER_DEFAULT_BATCH_SIZE below),
 *   never a client-chosen tuning.
 * - Ids travel as path parameters serialized by the generated client;
 *   bearer/tenant/idempotency secrets travel as HEADERS ONLY (1.6).
 * - MONEY (blocker (a)): every response figure is a server-computed
 *   decimal STRING asserted by the Zod boundary (numbers REJECTED).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  declarationSchema,
  dividendVoteResultSchema,
  distributionRunSchema,
  type DeclarationRecord,
  type DividendVote,
  type DividendVoteResult,
  type DistributionRun,
} from "./schemas";

export const DIVIDENDS_PAGE_SIZE = 20;

/** The SERVER's documented batch-size default (DeclareBody /
 * DistributeBody `@default 200` — backend
 * application/dividends.py:DEFAULT_BATCH_SIZE). The generated client
 * type requires the key on the wire, so it is sent VERBATIM as the
 * server documents it; this console never offers it as an operator
 * choice (it is a paging tuning, not a business parameter). */
export const SERVER_DEFAULT_BATCH_SIZE = 200;

const declarationPageSchema = keysetPageSchema(declarationSchema);

export async function fetchDeclarationsPage(
  cursor: string | null,
): Promise<KeysetPage<DeclarationRecord>> {
  const { data, error, response } = await api.GET("/dividends/declarations", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: DIVIDENDS_PAGE_SIZE,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return declarationPageSchema.parse(data);
}

/** Single declaration — the FRESH read (record class, staleTime 0)
 * behind every lifecycle write (transactions:view). */
export async function fetchDeclaration(declarationId: string): Promise<DeclarationRecord> {
  const { data, error, response } = await api.GET("/dividends/declarations/{declaration_id}", {
    params: { path: { declaration_id: declarationId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return declarationSchema.parse(data);
}

/**
 * Declare a dividend for the last completed financial year
 * (transactions:edit — back-office ledger governance, the gate decision). Rates and the FY period are resolved
 * server-side from tenant configuration; an
 * unconfigured tenant, an existing live declaration for the FY, or a
 * zero payout each refuse with 409 — nothing is defaulted. The body
 * carries only the server's own documented batch-size default (see
 * SERVER_DEFAULT_BATCH_SIZE).
 */
export async function declareDividend(idempotencyKey: string): Promise<DeclarationRecord> {
  const { data, error, response } = await api.POST("/dividends/declarations", {
    body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return declarationSchema.parse(data);
}

/**
 * Cast a committee vote on a declared dividend (transactions:approve —
 * the P9 quorum machinery; the server bans the declarer's self-vote
 * and enforces one vote per voter regardless of what this client
 * offers).
 */
export async function voteOnDeclaration(
  declarationId: string,
  vote: DividendVote,
  idempotencyKey: string,
): Promise<DividendVoteResult> {
  const { data, error, response } = await api.POST(
    "/dividends/declarations/{declaration_id}/votes",
    {
      params: { path: { declaration_id: declarationId } },
      body: { vote },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return dividendVoteResultSchema.parse(data);
}

/**
 * Void an open declaration (transactions:approve): declared/approved →
 * rejected, freeing the one-live-declaration-per-FY slot so a fresh
 * declaration captures current figures (the drift remedy). The
 * version pins the record state the operator acted on (1.4) — drift
 * is a 409, nothing changed. REFUSED by the server once any
 * distribution claim exists (re-declaring the year would pay
 * already-paid members twice).
 */
export async function voidDeclaration(
  declarationId: string,
  version: number,
  idempotencyKey: string,
): Promise<DeclarationRecord> {
  const { data, error, response } = await api.POST(
    "/dividends/declarations/{declaration_id}/void",
    {
      params: { path: { declaration_id: declarationId } },
      body: { version },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return declarationSchema.parse(data);
}

/**
 * Run the distribution job for an APPROVED declaration
 * (transactions:approve — the mass money-mover). Idempotent and
 * batched server-side: already-claimed members are skipped, mid-run
 * exits are disposed as unclaimed payables (P3, migration 0022), and concurrency-skipped members (`pending_members > 0`,
 * status still "approved") are picked up by an idempotent RE-RUN.
 * The body carries NO money field and NO version — only the server's
 * own documented batch-size default (see SERVER_DEFAULT_BATCH_SIZE):
 * every figure derives from the persisted approval snapshot, which
 * the first run re-verifies against live balances (any drift is a
 * 409 with NOTHING posted; the remedy is void + re-declare).
 */
export async function distributeDeclaration(
  declarationId: string,
  idempotencyKey: string,
): Promise<DistributionRun> {
  const { data, error, response } = await api.POST(
    "/dividends/declarations/{declaration_id}/distribution",
    {
      params: { path: { declaration_id: declarationId } },
      body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
      headers: { "Idempotency-Key": idempotencyKey },
    },
  );
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return distributionRunSchema.parse(data);
}
