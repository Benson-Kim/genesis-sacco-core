/**
   * Members API layer over the GENERATED client.
   *
   * - Keyset pagination ONLY: opaque `cursor` echoed back verbatim; no
   *   offset/page parameters exist here (scalability).
   * - The create mutation takes a caller-supplied Idempotency-Key following
   *   the stability/rotation contract (concurrency safety).
   * - Ids travel as path parameters serialized by the generated client;
   *   tokens/PII never enter URLs (least disclosure).
   */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
    dormancyRunSchema,
    memberDetailSchema,
    memberSchema,
    type DormancyRun,
    type Member,
    type MemberCreateInput,
    type MemberDetail,
    type MemberStatus,
    type MemberType,
} from "./schemas";

export const MEMBERS_PAGE_SIZE = 20;

const memberPageSchema = keysetPageSchema(memberSchema);
const memberDetailPageSchema = keysetPageSchema(memberDetailSchema);

export interface MemberListFilters {
    status: MemberStatus | "";
    type: MemberType | "";
}

export async function fetchMembersPage(
    filters: MemberListFilters,
    cursor: string | null,
): Promise<KeysetPage<Member>> {
    const { data, error, response } = await api.GET("/members", {
        params: {
            query: {
                cursor: cursor ?? undefined,
                limit: MEMBERS_PAGE_SIZE,
                status: filters.status === "" ? undefined : filters.status,
                type: filters.type === "" ? undefined : filters.type,
            },
        },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberPageSchema.parse(data);
}

/** Register page WITH the four advisory aggregates (the
 *  authorized OPT-IN expand): include=aggregates makes
 *  every row carry the same decimal-string figures as the detail
 *  read, from ONE set-based server statement per page. Discriminated
 *  fetch mirroring the fetchMember vs fetchMemberDetail split: every
 *  row's aggregates object is REQUIRED and parses strictly at the
 *  boundary, rendered VERBATIM (no client-side money
 *  math, ever); cross-module consumers that only resolve names keep
 *  the flat fetchMembersPage and can never render money. */
export async function fetchMembersPageWithAggregates(
    filters: MemberListFilters,
    cursor: string | null,
): Promise<KeysetPage<MemberDetail>> {
    const { data, error, response } = await api.GET("/members", {
        params: {
            query: {
                cursor: cursor ?? undefined,
                limit: MEMBERS_PAGE_SIZE,
                status: filters.status === "" ? undefined : filters.status,
                type: filters.type === "" ? undefined : filters.type,
                include: "aggregates",
            },
        },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberDetailPageSchema.parse(data);
}

/** Single member record (used by the applications detail drawer to
 *  resolve the applicant — the P9 list carries member_id only). */
/** Unique-identifier lookup (the posting drawer): the
 *  expand-only `member_no` EXACT-match param on GET /members, served
 *  by the DB UNIQUE (tenant_id, member_no). Least disclosure: the
 *  drawer renders only the resolved name + number; an unknown number
 *  is an honest null (the server serves an EMPTY page, never a 404). */
export async function lookupMemberByNo(memberNo: string): Promise<Member | null> {
    const { data, error, response } = await api.GET("/members", {
        params: { query: { limit: 1, member_no: memberNo } },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberPageSchema.parse(data).items[0] ?? null;
}

export async function fetchMember(memberId: string): Promise<Member> {
    const { data, error, response } = await api.GET("/members/{member_id}", {
        params: { path: { member_id: memberId } },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberSchema.parse(data);
}

/** Single-member DETAIL read (the register's KYC drawer): the same
 *  GET also carries the four advisory aggregate figures — decimal
 *  strings parsed strictly at the boundary and rendered VERBATIM
 *  (no client-side money math, ever). Cross-module
 *  consumers that only resolve a name keep using fetchMember. */
export async function fetchMemberDetail(memberId: string): Promise<MemberDetail> {
    const { data, error, response } = await api.GET("/members/{member_id}", {
        params: { path: { member_id: memberId } },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberDetailSchema.parse(data);
}

export async function createMember(
    input: MemberCreateInput,
    idempotencyKey: string,
): Promise<Member> {
    const { data, error, response } = await api.POST("/members", {
        body: input,
        headers: { "Idempotency-Key": idempotencyKey },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return memberSchema.parse(data);
}

/** The DormancyRunBody contract's own documented batch-size default
 * (@default 200) — a batch-SIZING tunable (the generated type requires
 * the key on the wire), never an operator or business value. */
export const SERVER_DORMANCY_BATCH_SIZE = 200;

/**
 * Run the dormancy job for the caller's tenant (
 * members:edit, the POST /jobs/arrears operations convention). The
 * body carries the server's documented batch-size default ONLY: the
 * dormancy period resolves exclusively from tenant settings
 * server-side (extra="forbid" — a period in the body is a 422) and
 * as_of is deliberately NOT sent, so the server resolves today. An
 * unconfigured or corrupt period REFUSES the run with 409 and zero
 * transitions (fail closed) — rendered, never retried.
 */
export async function runDormancyJob(idempotencyKey: string): Promise<DormancyRun> {
    const { data, error, response } = await api.POST("/members/jobs/dormancy", {
        body: { batch_size: SERVER_DORMANCY_BATCH_SIZE },
        headers: { "Idempotency-Key": idempotencyKey },
    });
    if (error !== undefined || data === undefined) throw toApiError(error, response);
    return dormancyRunSchema.parse(data);
}
