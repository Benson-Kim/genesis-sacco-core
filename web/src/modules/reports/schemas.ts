import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the reports module (module 8 — the exports API). Shapes mirror the generated client types
 * (components["schemas"]["ExportOut"], ArtifactOut, ExportCycleOut) —
 * the drift-checked OpenAPI snapshot remains the contract; these
 * schemas only assert it at runtime.
 *
 * SCOPE RULE (blocker (a), least disclosure): callers never supply money,
 * cost, format, row-limit or storage-path parameters. The ONLY
 * caller-suppliable scope is the id/date filter set the contract
 * declares (`ExportFiltersBody`); everything else — columns, caps,
 * rendering, storage — is frozen server-side at request time. This
 * module renders the server's export metadata verbatim and computes
 * NOTHING locally.
 */

/** Mirrors the backend ReportName enum (application/reports.py). This
 * is the REQUEST vocabulary: the client can only ever name a report
 * from this list (the generated ExportRequestBody type enforces it at
 * compile time; this runtime copy drives the catalogue). */
export const REPORT_NAMES = [
  "member_statement",
  "trial_balance",
  "loan_book",
  "disbursement_collections",
  "npl_trend",
  "member_exit_statement",
  "dividend_rebate_schedule",
  "portfolio_at_risk_aging",
  "membership_register",
  "income_statement",
  "sasra_return",
] as const;
export const reportNameSchema = z.enum(REPORT_NAMES);
export type ReportName = z.infer<typeof reportNameSchema>;

/** Operator-facing titles, mirroring the backend ReportDefinition
 * titles one-to-one (nothing renamed client-side). */
export const REPORT_LABELS: Record<ReportName, string> = {
  member_statement: "Member statement",
  trial_balance: "Trial balance",
  loan_book: "Loan book — classification & provisions",
  disbursement_collections: "Disbursements & collections",
  npl_trend: "NPL trend (monthly)",
  member_exit_statement: "Member exit statement",
  dividend_rebate_schedule: "Dividend & rebate schedule",
  portfolio_at_risk_aging: "Portfolio at risk — aging",
  membership_register: "Membership register",
  income_statement: "Income statement",
  sasra_return: "SASRA return (skeleton)",
};

/** The contract's filter vocabulary (ExportFiltersBody): ids and dates
 * ONLY — the sole caller-suppliable scope (blocker (a)). */
export const FILTER_KEYS = [
  "member_id",
  "exit_id",
  "declaration_id",
  "date_from",
  "date_to",
] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];

/**
 * Per-report filter surface, mirroring the backend REPORTS registry
 * (application/reports.py) — a UX pre-validation mirror ONLY: the
 * server re-validates (unknown filters and missing required filters
 * are a 422) and its verdict WINS per field. Filters a report does not
 * declare are never even offered (least surprise, least disclosure).
 */
export const REPORT_FILTERS: Record<
  ReportName,
  { allowed: readonly FilterKey[]; required: readonly FilterKey[] }
> = {
  member_statement: {
    allowed: ["member_id", "date_from", "date_to"],
    required: ["member_id"],
  },
  trial_balance: { allowed: [], required: [] },
  loan_book: { allowed: [], required: [] },
  disbursement_collections: { allowed: ["date_from", "date_to"], required: [] },
  npl_trend: { allowed: [], required: [] },
  member_exit_statement: { allowed: ["exit_id"], required: ["exit_id"] },
  dividend_rebate_schedule: {
    allowed: ["declaration_id"],
    required: ["declaration_id"],
  },
  portfolio_at_risk_aging: { allowed: [], required: [] },
  membership_register: { allowed: [], required: [] },
  income_statement: { allowed: ["date_from", "date_to"], required: [] },
  sasra_return: { allowed: [], required: [] },
};

/** ISO date SHAPE (YYYY-MM-DD) — the export date filters. Shape only:
 * calendar validity is `isCalendarDate` below (review). */
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Days per month, 1-indexed; February's 28 is corrected by the
 * leap-year arithmetic in `isCalendarDate`. */
const DAYS_IN_MONTH = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31] as const;

/**
 * PURE-STRING calendar validity for the date filters (review):
 * DATE_RE alone accepts impossible dates ("2026-02-31", "2026-13-01").
 * This checks the shape, then month 01–12 and a day valid for that
 * month, with leap years computed ARITHMETICALLY (divisible by 4,
 * except centuries unless divisible by 400) — no Date() construction
 * and no timezone math, so the verdict is deterministic everywhere.
 * A UX pre-validation mirror only: the server re-validates regardless.
 */
export function isCalendarDate(value: string): boolean {
  if (!DATE_RE.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  if (month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const maxDay = month === 2 && leap ? 29 : (DAYS_IN_MONTH[month] ?? 0);
  return day >= 1 && day <= maxDay;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The staged (all-string) filter values behind the request form. */
export type ExportFilterDraft = Record<FilterKey, string>;

export const EMPTY_FILTER_DRAFT: ExportFilterDraft = {
  member_id: "",
  exit_id: "",
  declaration_id: "",
  date_from: "",
  date_to: "",
};

export interface ExportEntry {
  report: ReportName;
  /** Only the provided keys travel — empty strings never do. */
  filters: Partial<Record<FilterKey, string>>;
}

const FILTER_MESSAGES: Record<FilterKey, string> = {
  member_id: "Select or enter the member (a UUID).",
  exit_id: "Select or enter the exit request (a UUID).",
  declaration_id: "Select or enter the dividend declaration (a UUID).",
  date_from: "Enter a real calendar date as YYYY-MM-DD.",
  date_to: "Enter a real calendar date as YYYY-MM-DD.",
};

/**
 * Client-side pre-validation of the export request (the server re-validates and is the enforcer — least disclosure): required filters per
 * the report's declared surface, UUID shape for ids, real calendar
 * dates (isCalendarDate — shape AND month/day/leap validity),
 * and an ordered date range. Returns the wire-ready entry or a
 * field→message map for FormField.
 */
export function validateExportDraft(
  report: ReportName,
  draft: ExportFilterDraft,
): { entry: ExportEntry; errors: null } | { entry: null; errors: Record<string, string> } {
  const surface = REPORT_FILTERS[report];
  const errors: Record<string, string> = {};
  const filters: Partial<Record<FilterKey, string>> = {};

  for (const key of surface.allowed) {
    const value = draft[key].trim();
    const required = surface.required.includes(key);
    if (value === "") {
      if (required) errors[key] = FILTER_MESSAGES[key];
      continue;
    }
    if ((key === "member_id" || key === "exit_id" || key === "declaration_id") && !UUID_RE.test(value)) {
      errors[key] = FILTER_MESSAGES[key];
      continue;
    }
    if ((key === "date_from" || key === "date_to") && !isCalendarDate(value)) {
      errors[key] = FILTER_MESSAGES[key];
      continue;
    }
    filters[key] = value;
  }

  const from = filters["date_from"];
  const to = filters["date_to"];
  // ISO date strings order lexicographically — a pure string check.
  if (from !== undefined && to !== undefined && to < from) {
    errors["date_to"] = "The end date must not precede the start date.";
  }

  if (Object.keys(errors).length > 0) return { entry: null, errors };
  return { entry: { report, filters }, errors: null };
}

/** The server's download paths are code-owned capability paths under
 * /exports/downloads/ with an unguessable urlsafe token — anything
 * else is a contract violation and is REJECTED at the boundary. */
const DOWNLOAD_PATH_RE = /^\/exports\/downloads\/[A-Za-z0-9_-]+$/;

/**
 * ISO-8601 datetime SHAPE for the export timestamps (review):
 * the SHARED shape from `@/lib/schemas` (single copy, reuse-first). These fields feed `fmtDateTime` on an
 * operator-facing audit surface; a garbage timestamp would render
 * "Invalid Date" there, so it is REJECTED at the boundary instead
 * (the module's reject-unknowns posture — consistent with
 * DOWNLOAD_PATH_RE and the int assertions).
 */
const timestampSchema = isoTimestampSchema;

/** ArtifactOut — every figure (row count, cap, truncation, expiry) is
 * the SERVER's render metadata, displayed verbatim. */
export const artifactSchema = z.object({
  row_count: z.number().int(),
  truncated: z.boolean(),
  row_limit: z.number().int(),
  expires_at: timestampSchema,
  csv_download: z.string().regex(DOWNLOAD_PATH_RE),
  pdf_download: z.string().regex(DOWNLOAD_PATH_RE),
});

export type Artifact = z.infer<typeof artifactSchema>;

/** ExportOut. Extra keys are STRIPPED at this boundary. `report` and
 * `status` are plain strings in the contract (ExportOut serialises the
 * enum server-side) — rendered verbatim, labelled when recognised. The
 * timestamps are ISO-shape-asserted so the audit surface can
 * never render "Invalid Date". */
export const exportOutSchema = z.object({
  id: z.string(),
  report: z.string(),
  status: z.string(),
  filters: z.record(z.string(), z.string()),
  allowed_columns: z.array(z.string()),
  as_of: timestampSchema,
  completed_at: timestampSchema.nullable(),
  created_at: timestampSchema,
  artifact: artifactSchema.nullable(),
});

export type ExportOut = z.infer<typeof exportOutSchema>;

/** ExportCycleOut — the queue-drain summary, server counts verbatim. */
export const exportCycleSchema = z.object({
  completed: z.number().int(),
  failed: z.number().int(),
});

export type ExportCycle = z.infer<typeof exportCycleSchema>;

/** Operator label for a witnessed export's report field: the known
 * catalogue title, or the server's raw value rendered as inert text. */
export function reportLabel(report: string): string {
  const parsed = reportNameSchema.safeParse(report);
  return parsed.success ? REPORT_LABELS[parsed.data] : report;
}

/**
 * Download-filename stem for an export artifact (review — filename injection/spoofing): `ExportOut.report` is an UNCONSTRAINED
 * string at this boundary, and `reportLabel` is display-only, so a
 * hostile/corrupted value could smuggle path separators, control
 * characters or an RTL override (U+202E) into the browser download
 * name. The stem is therefore ONLY ever the recognised enum value
 * ([a-z_] — filename-safe by construction, and exactly the stem the
 * server itself uses for its Content-Disposition filename) or a fixed
 * neutral fallback; the extension is appended last and fixed by the
 * caller. Raw server strings NEVER reach the download layer.
 */
export function exportFilenameStem(report: string): string {
  const parsed = reportNameSchema.safeParse(report);
  return parsed.success ? parsed.data : "export";
}

/**
 * Picker row for the exit filter (ExitOut, stripped): the reports
 * screen needs the id and enough context to pick one — none of the
 * record's settlement figures are consumed here (they belong to the
 * Member exit batch).
 */
export const exitPickerSchema = z.object({
  id: z.string(),
  member_id: z.string(),
  status: z.string(),
  created_at: z.string(),
});

export type ExitPickerRow = z.infer<typeof exitPickerSchema>;

/** Picker row for the declaration filter (DeclarationOut, stripped):
 * id + financial-year span + status only. */
export const declarationPickerSchema = z.object({
  id: z.string(),
  fy_start: z.string(),
  fy_end: z.string(),
  status: z.string(),
});

export type DeclarationPickerRow = z.infer<typeof declarationPickerSchema>;
