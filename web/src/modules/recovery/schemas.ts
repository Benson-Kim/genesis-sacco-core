import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the Recovery module (follow-on, — the collections & recovery worklist API). Shapes mirror the generated client types
 * (components["schemas"]["CaseOut"], WorklistRowOut, WorklistOut,
 * NoteOut, NotesOut) — the drift-checked OpenAPI snapshot remains the
 * contract; these schemas only assert it at runtime.
 *
 * MONEY (blocker (a)) — deliberately ABSENT: the contract is
 * LEAST DISCLOSURE by design (addendum): the worklist and case
 * records expose workflow fields, days-past-due and the classification
 * pill only — NO balance, penalty or provision figures (those live behind the loan-detail endpoints, for the entitled). No money
 * schema exists in this module because no money field exists on the
 * wire; a money-shaped addition here would be a contract violation,
 * not an improvement.
 *
 * TIMESTAMPS: every SLA timestamp (opened_at / first_assigned_at /
 * closed_at) is SERVER-written (addendum A7 — request bodies are
 * extra="forbid" and carry no time fields) and feeds fmtDateTime;
 * garbage is REJECTED at this boundary (an earlier review lesson).
 */

/** Mirrors the backend RecoveryCaseStatus enum (domain/recovery.py /
 * the 0033 six-state machine). An unknown value is a contract
 * violation and is REJECTED — an unrecognised status must never arm a
 * disposition write. */
export const CASE_STATUSES = [
  "open",
  "irrecoverable_pending_write_off",
  "disputed",
  "closed_cured",
  "closed_written_off",
  "closed_restructured",
] as const;
export const caseStatusSchema = z.enum(CASE_STATUSES);
export type CaseStatus = z.infer<typeof caseStatusSchema>;

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  open: "Open",
  irrecoverable_pending_write_off: "Paused — pending write-off",
  disputed: "Paused — disputed",
  closed_cured: "Closed — cured",
  closed_written_off: "Closed — written off",
  closed_restructured: "Closed — restructured",
};

/** The LIVE (non-closed) statuses: a live case blocks a duplicate case
 * on the same loan (0033 one-LIVE-case claim) and keeps its write
 * affordances; closed cases keep only the single outcome note. */
export const LIVE_CASE_STATUSES: readonly CaseStatus[] = [
  "open",
  "irrecoverable_pending_write_off",
  "disputed",
];

export function isLiveCaseStatus(status: CaseStatus): boolean {
  return LIVE_CASE_STATUSES.includes(status);
}

/**
 * DECLARED worklist filter vocabularies (the human-authorized read-contract expansion; until it, the worklist declared NO filter params and no filter UI existed). Code-owned
 * values ONLY, mirroring the contract's declared params exactly:
 * `status` narrows to one LIVE posture — exactly the backend
 * WorklistStatusParam Literal (the jest suite pins this array to
 * LIVE_CASE_STATUSES so the two can never drift) — and
 * `classification` to one STORED prudential label (the full LoanClass
 * set the worklist row accepts). The api layer sends a member of
 * these arrays or NOTHING; anything else is unrepresentable in the
 * generated client's types, and an out-of-vocabulary or undeclared
 * param stays the server's 422. Keyset and the server's
 * most-delinquent-first default ordering are preserved — nothing is
 * filtered or re-sorted locally.
 */
export const WORKLIST_STATUS_FILTERS = [
  "open",
  "irrecoverable_pending_write_off",
  "disputed",
] as const;
export type WorklistStatusFilter = (typeof WORKLIST_STATUS_FILTERS)[number];

export type WorklistClass = z.infer<typeof worklistClassSchema>;

/** The two staff PAUSE targets (a reason is REQUIRED on the record for both). */
export const PAUSE_TARGETS = ["disputed", "irrecoverable_pending_write_off"] as const;
export type PauseTarget = (typeof PAUSE_TARGETS)[number];

/**
 * The STAFF-settable disposition targets (the code-owned set): pause as disputed / irrecoverable-pending-write-off (reason
 * required), resume to open, or close as restructured (THE outcome
 * note required, written atomically). closed_cured and
 * closed_written_off are deliberately ABSENT: loan facts close those
 * (the nightly arrears pass) — the officer can never hand-declare a
 * cure or a write-off, and this client never offers what the API
 * refuses (409).
 */
export const STAFF_DISPOSITION_TARGETS = [
  "disputed",
  "irrecoverable_pending_write_off",
  "open",
  "closed_restructured",
] as const;
export type StaffDispositionTarget = (typeof STAFF_DISPOSITION_TARGETS)[number];

export const DISPOSITION_TARGET_LABELS: Record<StaffDispositionTarget, string> = {
  disputed: "Pause — member disputes the arrears",
  irrecoverable_pending_write_off: "Pause — irrecoverable, committee write-off pending",
  open: "Resume working the case",
  closed_restructured: "Close — the loan was restructured",
};

/** The NPL classes a case can OPEN at (0026 CHECK: > 90 DPD) — a
 * strict subset of the loans vocabulary; the WORKLIST's live
 * classification may have improved since open, so it accepts the FULL
 * LoanClass set (a paying loan can drop bands before the nightly
 * close pass cures the case). */
export const OPEN_CLASSES = ["substandard", "doubtful", "loss"] as const;
export const openClassSchema = z.enum(OPEN_CLASSES);

export const WORKLIST_CLASSES = ["normal", "watch", "substandard", "doubtful", "loss"] as const;
export const worklistClassSchema = z.enum(WORKLIST_CLASSES);

/**
 * One recovery case (CaseOut). ATTRIBUTION (the pattern):
 * opened_by is REQUIRED server truth; assignee_id is nullable (never
 * optional). Both render under LEAST DISCLOSURE as the bare staff UUID
 * via the short-id convention — never a name or email; this client
 * NEVER fetches a directory record for them.
 */
export const caseSchema = z.object({
  id: z.string(),
  loan_id: z.string(),
  status: caseStatusSchema,
  assignee_id: z.string().nullable(),
  opened_by: z.string(),
  classification_at_open: openClassSchema,
  days_past_due_at_open: z.number().int(),
  opened_at: isoTimestampSchema,
  first_assigned_at: isoTimestampSchema.nullable(),
  closed_at: isoTimestampSchema.nullable(),
  /** Optimistic-lock version pinning assign/disposition writes (1.4). */
  version: z.number().int(),
});

export type CaseRecord = z.infer<typeof caseSchema>;

/** One worklist row (WorklistRowOut — least disclosure, addendum):
 * workflow fields, days-past-due and the classification pill only —
 * NO balances (the loan screens carry those, for the entitled). */
export const worklistRowSchema = z.object({
  case_id: z.string(),
  loan_id: z.string(),
  member_id: z.string(),
  days_past_due: z.number().int(),
  classification: worklistClassSchema,
  assignee_id: z.string().nullable(),
  /** Addendum: TRUE when the assignee was suspended AFTER
   * assignment — the case stays visible, flagged, never orphaned. */
  assignee_unassignable: z.boolean(),
  opened_at: isoTimestampSchema,
  first_assigned_at: isoTimestampSchema.nullable(),
  version: z.number().int(),
});

export type WorklistRow = z.infer<typeof worklistRowSchema>;

/** One case note (NoteOut — append-only, addendum: no edit/delete
 * route exists; the file is evidence). author_id is REQUIRED server
 * truth, rendered under least disclosure. is_outcome marks THE single
 * post-closure outcome note. */
export const noteSchema = z.object({
  id: z.string(),
  author_id: z.string(),
  note: z.string(),
  created_at: isoTimestampSchema,
  is_outcome: z.boolean(),
});

export type CaseNote = z.infer<typeof noteSchema>;

/**
 * Client-side pre-validation of the entry forms (the server re-validates and is the enforcer — least disclosure). NO money field exists
 * anywhere in this module's inputs, and NO timestamp can even be sent
 * (addendum A7).
 */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const openCaseEntrySchema = z.object({
  loan_id: z
    .string()
    .min(1, "The loan id is required.")
    .regex(UUID_RE, "Enter the full UUID (8-4-4-4-12 hex)."),
});

export type OpenCaseEntry = z.infer<typeof openCaseEntrySchema>;

export const assignEntrySchema = z.object({
  assignee_id: z
    .string()
    .min(1, "Select or enter the assignee.")
    .regex(UUID_RE, "Enter the full user UUID (8-4-4-4-12 hex)."),
});

export const noteEntrySchema = z.object({
  note: z
    .string()
    .min(1, "The note text is required.")
    .max(2000, "Keep the note under 2000 characters."),
});

/** Target-paired disposition entry: the reason is required
 * for the two pause targets, the note for the restructure close;
 * resume carries neither. The server re-validates the pairing (422 on
 * mismatch) and the move's legality (409 via the single gatekeeper). */
export const dispositionEntrySchema = z
  .object({
    status: z.enum(STAFF_DISPOSITION_TARGETS, { message: "Select the disposition." }),
    reason: z.string().max(500, "Keep the reason under 500 characters."),
    note: z.string().max(2000, "Keep the outcome note under 2000 characters."),
  })
  .superRefine((entry, ctx) => {
    if (
      (entry.status === "disputed" || entry.status === "irrecoverable_pending_write_off") &&
      entry.reason.trim() === ""
    ) {
      ctx.addIssue({
        code: "custom",
        path: ["reason"],
        message: "A reason is REQUIRED to pause a case (it goes on the audit record).",
      });
    }
    if (entry.status === "closed_restructured" && entry.note.trim() === "") {
      ctx.addIssue({
        code: "custom",
        path: ["note"],
        message:
          "THE closing outcome note is REQUIRED to close as restructured (written atomically with the close).",
      });
    }
  });

export type DispositionEntry = z.infer<typeof dispositionEntrySchema>;
