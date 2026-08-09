/**
 * Branches registry API layer (the registry contract) over the GENERATED client.
 *
 * - Keyset pagination ONLY on the register read (settings:view): the
 *   opaque `cursor` is echoed back verbatim; no offset or page
 *   parameters exist here (scalability). The list contract declares NO
 *   filters — the client sends none and filters nothing locally.
 * - Permission map (the P4 matrix as recorded on the router): registry
 *   reads settings:view, create settings:create, rename settings:edit,
 *   the backfill job settings:edit — but ASSIGNMENTS follow the entity
 *   being edited, not the registry: assigning a USER is
 *   access_control:edit (the users precedent), assigning a
 *   MEMBER is members:edit (the precedent). Settings rights alone
 *   must not allow editing people records; the UI mirrors exactly
 *   that split (the server enforces regardless, least disclosure).
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the web/README.md MATERIAL rule (concurrency safety), travelling as a
 *   HEADER only. Rename pins the branch `version`; each assignment
 *   pins the USER/MEMBER row's `version` (a stale write is a 409 with
 *   NOTHING changed — the explicit reload-and-re-enter flow, never a
 *   replay, never a silent overwrite).
 * - NO money parameter exists in ANY request body (the router: branch
 *   is organisational metadata only; extra="forbid" server-side). The
 *   backfill body's lone knob, batch_size, is a server tuning
 *   parameter — the generated type requires the key on the wire, so
 *   this console sends the SERVER's own documented default verbatim
 *   (SERVER_DEFAULT_BATCH_SIZE below), never an operator choice.
 * - Ids travel as path parameters serialized by the generated client;
 *   bearer/tenant/idempotency secrets travel as HEADERS ONLY (1.6).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  backfillRunSchema,
  branchAssignmentSchema,
  branchMemberRosterSchema,
  branchSchema,
  branchUserRosterSchema,
  type BackfillRun,
  type BranchAssignment,
  type BranchMemberRosterRow,
  type BranchRecord,
  type BranchUserRosterRow,
} from "./schemas";

export const BRANCHES_PAGE_SIZE = 20;
export const ROSTER_PAGE_SIZE = 20;

/** The SERVER's documented batch-size default (BackfillRunBody
 * `@default 200` — backend application/branches.py:DEFAULT_BATCH_SIZE).
 * The generated client type requires the key on the wire, so it is
 * sent VERBATIM as the server documents it; this console never offers
 * it as an operator choice (a paging tuning, not a business value). */
export const SERVER_DEFAULT_BATCH_SIZE = 200;

const branchPageSchema = keysetPageSchema(branchSchema);
const userRosterPageSchema = keysetPageSchema(branchUserRosterSchema);
const memberRosterPageSchema = keysetPageSchema(branchMemberRosterSchema);

/** Keyset register read, newest first (server ordering). */
export async function fetchBranchesPage(cursor: string | null): Promise<KeysetPage<BranchRecord>> {
  const { data, error, response } = await api.GET("/branches", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: BRANCHES_PAGE_SIZE,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchPageSchema.parse(data);
}

/** Single branch — the FRESH read (record class, staleTime 0) behind
 * the version-pinned rename (settings:view). */
export async function fetchBranch(branchId: string): Promise<BranchRecord> {
  const { data, error, response } = await api.GET("/branches/{branch_id}", {
    params: { path: { branch_id: branchId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchSchema.parse(data);
}

/**
 * Keyset USER roster of one branch ((j).1 — access_control:view, the assignment permission split mirrored on reads: the entity being READ is the user record, never a settings right).
 * Identity facts only; 404-before-facts server-side (an unknown or
 * cross-tenant branch id is a 404, never an empty page).
 */
export async function fetchBranchUsersRosterPage(
  branchId: string,
  cursor: string | null,
): Promise<KeysetPage<BranchUserRosterRow>> {
  const { data, error, response } = await api.GET("/branches/{branch_id}/users", {
    params: {
      path: { branch_id: branchId },
      query: { cursor: cursor ?? undefined, limit: ROSTER_PAGE_SIZE },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return userRosterPageSchema.parse(data);
}

/**
 * Keyset MEMBER roster of one branch ((j).1 — members:view, the split's member leg). Identity facts only; 404-before-facts
 * server-side.
 */
export async function fetchBranchMembersRosterPage(
  branchId: string,
  cursor: string | null,
): Promise<KeysetPage<BranchMemberRosterRow>> {
  const { data, error, response } = await api.GET("/branches/{branch_id}/members", {
    params: {
      path: { branch_id: branchId },
      query: { cursor: cursor ?? undefined, limit: ROSTER_PAGE_SIZE },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return memberRosterPageSchema.parse(data);
}

/** Register a branch (settings:create). A duplicate name (after the
 * server's whitespace normalisation) is its 409 — nothing created. */
export async function createBranch(name: string, idempotencyKey: string): Promise<BranchRecord> {
  const { data, error, response } = await api.POST("/branches", {
    body: { name },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchSchema.parse(data);
}

/** Rename a branch (settings:edit), version-pinned: drift since the
 * operator's fresh read — or a name collision — is a 409 with NOTHING
 * changed. */
export async function updateBranch(
  branchId: string,
  version: number,
  name: string,
  idempotencyKey: string,
): Promise<BranchRecord> {
  const { data, error, response } = await api.PUT("/branches/{branch_id}", {
    params: { path: { branch_id: branchId } },
    body: { version, name },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchSchema.parse(data);
}

/** Assign a USER to a branch (access_control:edit — the users precedent). The body pins the USER row's version. */
export async function assignUserToBranch(
  branchId: string,
  userId: string,
  version: number,
  idempotencyKey: string,
): Promise<BranchAssignment> {
  const { data, error, response } = await api.PUT("/branches/{branch_id}/users/{user_id}", {
    params: { path: { branch_id: branchId, user_id: userId } },
    body: { version },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchAssignmentSchema.parse(data);
}

/** Assign a MEMBER to a branch (members:edit — the precedent). The
 * body pins the MEMBER row's version. */
export async function assignMemberToBranch(
  branchId: string,
  memberId: string,
  version: number,
  idempotencyKey: string,
): Promise<BranchAssignment> {
  const { data, error, response } = await api.PUT("/branches/{branch_id}/members/{member_id}", {
    params: { path: { branch_id: branchId, member_id: memberId } },
    body: { version },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return branchAssignmentSchema.parse(data);
}

/**
 * Run the legacy-text branch backfill for the caller's own tenant
 * (settings:edit — the registry it populates). Batched and idempotent
 * server-side: a completed re-run scans nothing (anti-join on the
 * claim key — a lock-free no-op proven by the returned counts). The
 * body carries only the server's own documented batch-size default.
 */
export async function runBranchBackfill(idempotencyKey: string): Promise<BackfillRun> {
  const { data, error, response } = await api.POST("/jobs/branch-backfill", {
    body: { batch_size: SERVER_DEFAULT_BATCH_SIZE },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return backfillRunSchema.parse(data);
}
