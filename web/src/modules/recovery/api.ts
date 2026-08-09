/**
 * Recovery API layer (follow-on, — the collections & recovery-case API) over the GENERATED client.
 *
 * - Keyset pagination ONLY on the worklist and notes reads (scalability):
 *   opaque `cursor` echoed back verbatim; no offset/page parameters
 *   exist. The worklist sends ONLY the two DECLARED filter params
 *   (the human-authorized read-contract expansion): `status` (one LIVE posture) and `classification` (one
 *   stored LoanClass label), each a code-owned vocabulary member or
 *   ABSENT — never free text; the server keeps most-delinquent-first
 *   default ordering and nothing is filtered or re-sorted locally.
 * - Every mutation takes a caller-supplied Idempotency-Key following
 *   the stability/rotation contract (concurrency safety). Assign and disposition
 *   PIN the record version in the wire body: any drift since the
 *   operator's fresh read is a 409 with nothing changed.
 * - NO money parameter exists in ANY request body, and NO timestamp
 *   can even be sent (addendum A7: opened_at/first_assigned_at/
 *   closed_at are server-written; extra="forbid"). The disposition
 *   body carries the target-paired rationale fields only;
 *   keys the target does not require are NOT sent at all.
 * - There is NO cure/write-off close route and NO note edit/delete
 *   route on the contract (append-only, addendum) — this layer
 *   exposes exactly what exists, nothing more.
 * - Ids travel as path parameters serialized by the generated client;
 *   bearer/tenant/idempotency secrets travel as HEADERS ONLY.
 */
import { toApiError } from "@genesis/api-client";
import { keysetPageSchema, type KeysetPage } from "@/modules/table/schemas";
import { api } from "@/lib/api";
import {
  caseSchema,
  noteSchema,
  worklistRowSchema,
  type CaseNote,
  type CaseRecord,
  type DispositionEntry,
  type WorklistClass,
  type WorklistRow,
  type WorklistStatusFilter,
} from "./schemas";

export const WORKLIST_PAGE_SIZE = 20;
export const NOTES_PAGE_SIZE = 20;

const worklistPageSchema = keysetPageSchema(worklistRowSchema);
const notesPageSchema = keysetPageSchema(noteSchema);

/** The two DECLARED worklist filters (the members-register precedent): each is a code-owned vocabulary member
 * or "" — "" sends NOTHING (the parameter is ABSENT from the URL, so
 * the no-filter request stays byte-identical to the as-built read). */
export interface WorklistFilters {
  status: WorklistStatusFilter | "";
  classification: WorklistClass | "";
}

/** The recovery-officer worklist (loan_book:view): keyset, most
 * delinquent first — the server's order; never re-sorted locally.
 * Filters are the two DECLARED params only, every value bound
 * server-side. */
export async function fetchWorklistPage(
  filters: WorklistFilters,
  cursor: string | null,
): Promise<KeysetPage<WorklistRow>> {
  const { data, error, response } = await api.GET("/recovery-cases", {
    params: {
      query: {
        cursor: cursor ?? undefined,
        limit: WORKLIST_PAGE_SIZE,
        status: filters.status === "" ? undefined : filters.status,
        classification: filters.classification === "" ? undefined : filters.classification,
      },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return worklistPageSchema.parse(data);
}

/** Single case record — the FRESH read (record class, staleTime 0)
 * behind every case write (loan_book:view). */
export async function fetchCase(caseId: string): Promise<CaseRecord> {
  const { data, error, response } = await api.GET("/recovery-cases/{case_id}", {
    params: { path: { case_id: caseId } },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return caseSchema.parse(data);
}

/**
 * Initiate recovery on a non-performing active loan (loan_book:create
 * — the prototype's "Initiate recovery" action). The body carries the
 * loan id ONLY: the NPL snapshot is read under the loan row lock and
 * every SLA timestamp is server-written (addendum A7).
 */
export async function openCase(loanId: string, idempotencyKey: string): Promise<CaseRecord> {
  const { data, error, response } = await api.POST("/recovery-cases", {
    body: { loan_id: loanId },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return caseSchema.parse(data);
}

/**
 * Assign/reassign an open case (loan_book:edit): the assignee must be
 * an ACTIVE same-tenant user holding the collections grant, EXCLUDING
 * assurance roles (audit independence — all server-vetted; this
 * client sends the id and lets the server judge). The version
 * pins the record state the operator acted on (1.4).
 */
export async function assignCase(
  caseId: string,
  version: number,
  assigneeId: string,
  idempotencyKey: string,
): Promise<CaseRecord> {
  const { data, error, response } = await api.POST("/recovery-cases/{case_id}/assign", {
    params: { path: { case_id: caseId } },
    body: { version, assignee_id: assigneeId },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return caseSchema.parse(data);
}

/**
 * Record a staff disposition (loan_book:edit): pause (reason
 * REQUIRED), resume, or close as restructured
 * (outcome note REQUIRED, written atomically server-side). The body
 * carries ONLY the keys the target requires — a pause never smuggles a
 * note, a resume sends neither (the pairing is server-enforced with a
 * 422; job-only targets are a 409 through the single gatekeeper).
 */
export async function setDisposition(
  caseId: string,
  version: number,
  entry: DispositionEntry,
  idempotencyKey: string,
): Promise<CaseRecord> {
  const body: {
    version: number;
    status: DispositionEntry["status"];
    reason?: string;
    note?: string;
  } = { version, status: entry.status };
  if (entry.status === "disputed" || entry.status === "irrecoverable_pending_write_off") {
    body.reason = entry.reason.trim();
  }
  if (entry.status === "closed_restructured") {
    body.note = entry.note.trim();
  }
  const { data, error, response } = await api.POST("/recovery-cases/{case_id}/disposition", {
    params: { path: { case_id: caseId } },
    body,
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return caseSchema.parse(data);
}

/** Append a permanent note to the case file (loan_book:edit; addendum
 * append-only — no edit or delete route exists). */
export async function addCaseNote(
  caseId: string,
  note: string,
  idempotencyKey: string,
): Promise<CaseNote> {
  const { data, error, response } = await api.POST("/recovery-cases/{case_id}/notes", {
    params: { path: { case_id: caseId } },
    body: { note },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return noteSchema.parse(data);
}

/** Record THE post-closure outcome note (loan_book:edit): exactly one per case, at/after closure only — a second attempt
 * is the server's 409, never a client-side pretence. */
export async function addOutcomeNote(
  caseId: string,
  note: string,
  idempotencyKey: string,
): Promise<CaseNote> {
  const { data, error, response } = await api.POST("/recovery-cases/{case_id}/outcome-note", {
    params: { path: { case_id: caseId } },
    body: { note },
    headers: { "Idempotency-Key": idempotencyKey },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return noteSchema.parse(data);
}

/** The case file (loan_book:view): keyset notes, append-only. */
export async function fetchCaseNotesPage(
  caseId: string,
  cursor: string | null,
): Promise<KeysetPage<CaseNote>> {
  const { data, error, response } = await api.GET("/recovery-cases/{case_id}/notes", {
    params: {
      path: { case_id: caseId },
      query: { cursor: cursor ?? undefined, limit: NOTES_PAGE_SIZE },
    },
  });
  if (error !== undefined || data === undefined) throw toApiError(error, response);
  return notesPageSchema.parse(data);
}
