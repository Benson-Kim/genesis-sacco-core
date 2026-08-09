/**
 * Reports API layer (module 8 — the exports API) over the
 * GENERATED client.
 *
 * - The contract exposes the EXPORT workflow only: request
 *   (`POST /exports`), requester-only status (`GET /exports/{id}`),
 *   artifact streaming (`GET /exports/downloads/{token}`) and the
 *   operations queue drain (`POST /jobs/exports`). There is NO
 *   in-browser report-viewing endpoint and NO export list endpoint —
 *   neither is invented client-side (recorded honestly in the MR).
 * - Callers never supply money, cost, format, row-limit or
 *   storage-path parameters (blocker (a)): the request body
 *   carries the report name and its declared id/date filters ONLY
 *   (`extra="forbid"` server-side); empty filters are OMITTED, never
 *   sent as empty strings.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the stability/rotation contract (concurrency safety). A 409 is a
 *   CREATE-style conflict (no export record exists to reload) —
 *   surfaced once, never replayed.
 * - Ids and download tokens travel as PATH parameters serialized by
 *   the generated client; bearer/tenant/idempotency secrets travel as
 *   HEADERS ONLY — nothing secret enters a hand-built URL (least disclosure; downloads ride the shared `downloadExport` helper on the generated client's Response).
 * - The picker reads for the id filters ride the existing keyset
 *   contracts (`GET /member-exits`, `GET /dividends/declarations`)
 *   with opaque cursors echoed verbatim (scalability); their money fields
 *   are STRIPPED at this module's boundary — nothing here renders a
 *   figure.
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import { downloadExport } from "@/lib/file-export";
import {
  declarationPickerSchema,
  exitPickerSchema,
  exportCycleSchema,
  exportOutSchema,
  type DeclarationPickerRow,
  type ExitPickerRow,
  type ExportCycle,
  type ExportEntry,
  type ExportOut,
} from "./schemas";

export const PICKER_PAGE_SIZE = 20;

const exitPageSchema = keysetPageSchema(exitPickerSchema);
const declarationPageSchema = keysetPageSchema(declarationPickerSchema);

/**
 * Queue an export (reports:view — an export is a materialised READ of
 * the reports module; scope, columns and entitlement are frozen
 * server-side at request time). Only provided filters travel.
 */
export async function requestExport(
  entry: ExportEntry,
  idempotencyKey: string,
): Promise<ExportOut> {
  const filterKeys = Object.keys(entry.filters);
  const { data, error, response } = await api.POST("/exports", {
    body: {
      report: entry.report,
      filters:
        filterKeys.length === 0
          ? undefined
          : {
              member_id: entry.filters.member_id,
              exit_id: entry.filters.exit_id,
              declaration_id: entry.filters.declaration_id,
              date_from: entry.filters.date_from,
              date_to: entry.filters.date_to,
            },
    },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exportOutSchema.parse(data);
}

/** Status + download links for ONE export — requester-only
 * server-side (least disclosure): another operator's export id 404s. */
export async function fetchExport(exportId: string): Promise<ExportOut> {
  const { data, error, response } = await api.GET("/exports/{export_id}", {
    params: { path: { export_id: exportId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exportOutSchema.parse(data);
}

/**
 * Drain the tenant export queue (`POST /jobs/exports`, reports:view —
 * the operations mirror of the worker loop; each job renders in its
 * own snapshot transaction server-side). The contract defines NO
 * request body — nothing tunable can even be sent.
 */
export async function runExportQueue(idempotencyKey: string): Promise<ExportCycle> {
  const { data, error, response } = await api.POST("/jobs/exports", {
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exportCycleSchema.parse(data);
}

const DOWNLOAD_PREFIX = "/exports/downloads/";

/** Extract the capability token from a boundary-validated download
 * path (the Zod boundary already rejected any other shape). */
export function downloadToken(downloadPath: string): string {
  if (!downloadPath.startsWith(DOWNLOAD_PREFIX)) {
    throw new Error("unexpected export download path");
  }
  return downloadPath.slice(DOWNLOAD_PREFIX.length);
}

/**
 * Stream a rendered artifact through the GENERATED client and hand the
 * Response to the shared `downloadExport` helper (one copy, reuse-first):
 * the token rides the contract's path parameter; auth/tenant stay
 * HEADERS; failures surface least-disclosure; the object URL is
 * transient and revoked.
 */
export function downloadArtifact(downloadPath: string, filename: string): Promise<void> {
  const token = downloadToken(downloadPath);
  return downloadExport(async () => {
    const { response } = await api.GET("/exports/downloads/{token}", {
      params: { path: { token } },
      parseAs: "stream",
    });
    return response;
  }, filename);
}

/** The on-screen view's artifact payload: the server-rendered CSV
 * TEXT plus the truncation headers VERBATIM. */
export interface ArtifactText {
  text: string;
  /** X-Export-Truncated response header, verbatim ("true"/"false"). */
  truncatedHeader: string | null;
  /** X-Export-Limit response header, verbatim. */
  limitHeader: string | null;
}

/**
 * Fetch a rendered CSV artifact as TEXT for the on-screen report view
 * (ZERO contract change: the same capability-token download route
 * the CSV button uses, through the GENERATED client; bearer/tenant
 * stay HEADERS). The body is the server's own render; the caller
 * parses it strictly (csv.ts) and renders every cell VERBATIM — the
 * download path stays the canonical artifact. Failures surface as the
 * least-disclosure ApiError; the body is never echoed.
 */
export async function fetchArtifactText(downloadPath: string): Promise<ArtifactText> {
  const token = downloadToken(downloadPath);
  const { response } = await api.GET("/exports/downloads/{token}", {
    params: { path: { token } },
    parseAs: "stream",
  });
  if (!response.ok) {
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw toApiError(body, response);
  }
  return {
    text: await response.text(),
    truncatedHeader: response.headers.get("X-Export-Truncated"),
    limitHeader: response.headers.get("X-Export-Limit"),
  };
}

/** Keyset picker read for the exit_id filter (members:view — the
 * caller mounts this ONLY when the grant is present). */
export async function fetchExitsPage(cursor: string | null): Promise<KeysetPage<ExitPickerRow>> {
  const { data, error, response } = await api.GET("/member-exits", {
    params: { query: { cursor: cursor ?? undefined, limit: PICKER_PAGE_SIZE } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return exitPageSchema.parse(data);
}

/** Keyset picker read for the declaration_id filter (transactions:view
 * — the caller mounts this ONLY when the grant is present). */
export async function fetchDeclarationsPage(
  cursor: string | null,
): Promise<KeysetPage<DeclarationPickerRow>> {
  const { data, error, response } = await api.GET("/dividends/declarations", {
    params: { query: { cursor: cursor ?? undefined, limit: PICKER_PAGE_SIZE } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return declarationPageSchema.parse(data);
}
