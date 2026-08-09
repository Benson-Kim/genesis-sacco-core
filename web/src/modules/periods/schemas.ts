import { z } from "zod";
import { isoTimestampSchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the accounting-periods console
 * (the period machinery;: "period-close state invisible in the console"). Shapes mirror
 * the generated client types (components["schemas"]["PeriodOut"]) —
 * the drift-checked OpenAPI snapshot remains the contract; these
 * schemas only assert it at runtime.
 *
 * NO MONEY FIELD exists on this contract (honesty over decoration):
 * a period row is pure ledger governance metadata — bounds, status,
 * close stamp, version. The shared moneySchema is deliberately NOT
 * imported here; asserting a money shape that the contract does not
 * carry would be fake validation.
 *
 * Field-by-field shape derivation (backend/src/genesis, not guessed):
 * - period_start / period_end: DATE columns (migration 0012) serialised
 *   via date.isoformat() (api/accounting_periods.py:_out) →
 *   "YYYY-MM-DD" exactly (`periodDateSchema`) — calendar dates, not
 *   instants; feeding them to fmtDateTime would invent a time of day.
 * - closed_at: datetime.isoformat() or the honest NULL (the column is
 *   nullable; the API serialises it only when set) →
 *   `isoTimestampSchema.nullable()` — nullable, NOT optional.
 * - status: migration 0012's DB CHECK constrains the vocabulary to
 *   ('open', 'closed') and the service only ever writes 'closed'
 *   (absence of a row = the month is open — design). An
 *   unknown value is a contract violation REJECTED at the boundary
 *   (the W57-2 fleet standard) — an unrecognised status must never
 *   masquerade as period context next to ledger figures. The contract
 *   exposes no status enum to compile-time pin, so the vocabulary is
 *   pinned to the migration SOURCE OF TRUTH by the drift-tripwire leg
 *   in __tests__/periods-api.network.test.ts (the W57-5 pattern).
 * - version: optimistic-lock integer (kept for forward compatibility;
 *   the close body itself carries NO version — the year/month slot is
 *   the server's own idempotent claim, see api.ts).
 */
export const PERIOD_STATUSES = ["open", "closed"] as const;
export const periodStatusSchema = z.enum(PERIOD_STATUSES);
export type PeriodStatus = z.infer<typeof periodStatusSchema>;

export const PERIOD_STATUS_LABELS: Record<PeriodStatus, string> = {
  open: "Open",
  closed: "Closed",
};

/** DATE column via date.isoformat(): "YYYY-MM-DD" exactly. */
export const PERIOD_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const periodDateSchema = z.string().regex(PERIOD_DATE_RE);

/**
 * One accounting period row (PeriodOut): a month whose books the
 * transactions-approve holders froze. Every posting-date check, the
 * period bounds and the "fully elapsed" rule are resolved exclusively
 * server-side (never caller-backdatable). Extra keys are
 * STRIPPED at this boundary.
 */
export const periodSchema = z.object({
  id: z.string(),
  period_start: periodDateSchema,
  period_end: periodDateSchema,
  status: periodStatusSchema,
  closed_at: isoTimestampSchema.nullable(),
  version: z.number().int(),
});

export type PeriodRecord = z.infer<typeof periodSchema>;

/* ------------------------------------------------------------------ *
 * Close-period form entry (client-side shape only — the server owns
 * the "fully elapsed" verdict and every bound; a violating month is
 * its 409/422, rendered, never pre-empted beyond shape).
 * The contract bounds (ClosePeriodBody): year 2000..2100, month 1..12.
 * ------------------------------------------------------------------ */
export const closePeriodEntrySchema = z.object({
  year: z.number().int().min(2000).max(2100),
  month: z.number().int().min(1).max(12),
});

export type ClosePeriodEntry = z.infer<typeof closePeriodEntrySchema>;

/** Month names for the close-period picker (calendar vocabulary only —
 * the server resolves every bound; index 0 = January = month 1). */
export const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
] as const;
