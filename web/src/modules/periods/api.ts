/**
 * Accounting-periods API layer (the period machinery) over the GENERATED client.
 *
 * - Keyset pagination ONLY on the register read (transactions:view):
 *   the opaque `cursor` is echoed back verbatim; no offset or page
 *   parameters exist here (scalability). The list contract declares NO
 *   filters — the client sends none and filters nothing locally.
 * - The ONLY mutation is close (transactions:approve — the P4-matrix
 *   decision recorded on the router: the people who post must not be
 *   able to freeze the periods they post into; deliberately NOT
 *   maker-checker on the contract, so no MakerCheckerPanel is
 *   consumed — a fake checker leg would be dishonest UI). The body
 *   carries ONLY the calendar year/month; the period bounds, the
 *   "fully elapsed" rule and every posting-date check are resolved
 *   server-side (never caller-backdatable); extra="forbid"
 *   server-side turns anything else into a 422. There is NO version
 *   field on this contract: the (tenant, period_start) slot is the
 *   server's own idempotent claim — concurrent closes collapse to one
 *   closed row and the loser gets a clean 409.
 * - There is NO "open"/reopen mutation on the contract: absence of a
 *   row IS the open state (design) — this console renders
 *   that honestly instead of inventing a reopen affordance.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the web/README.md MATERIAL rule (concurrency safety), travelling as a
 *   HEADER only (least disclosure).
 * - NO money field exists anywhere on this contract (see schemas.ts).
 *
 * DELIBERATELY NOT SURFACED here: POST /jobs/portfolio-snapshots and
 * POST /jobs/period-rollups (.17a/b) share the router file but are
 * one-off migration-era backfill jobs, not period visibility/close
 * governance — out of this batch's recorded scope (noted on the MR).
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import { periodSchema, type PeriodRecord } from "./schemas";

export const PERIODS_PAGE_SIZE = 20;

const periodPageSchema = keysetPageSchema(periodSchema);

/** Keyset register read, newest first (server ordering). */
export async function fetchPeriodsPage(cursor: string | null): Promise<KeysetPage<PeriodRecord>> {
  const { data, error, response } = await api.GET("/accounting-periods", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: PERIODS_PAGE_SIZE,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return periodPageSchema.parse(data);
}

/**
 * Close a fully elapsed calendar month (transactions:approve). The
 * server refuses the current/future month, a malformed month and an
 * already-closed month (409 — nothing changed); success freezes every
 * posting date inside the bounds (the 0012 trigger enforces it at the
 * database regardless of any API path).
 */
export async function closePeriod(
  year: number,
  month: number,
  idempotencyKey: string,
): Promise<PeriodRecord> {
  const { data, error, response } = await api.POST("/accounting-periods/close", {
    body: { year, month },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return periodSchema.parse(data);
}
