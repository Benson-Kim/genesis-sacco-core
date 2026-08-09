import { z } from "zod";
import { moneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the guarantors module (module 5 — the P9/ guarantee-pledging surface). Shapes mirror the
 * generated client types (components["schemas"]["GuaranteeOut"] etc.) —
 * the drift-checked OpenAPI snapshot remains the contract; these
 * schemas only assert it at runtime.
 *
 * MONEY RULE: every monetary field (pledge amount,
 * pledged totals, free capacity) is a decimal STRING end-to-end,
 * rendered via fmtKes or verbatim and never coerced or combined
 * arithmetically. Guarantor capacity, encumbrance and exposure are
 * SERVER facts (the P9 capacity check runs under the guarantor's deposit-account row lock; the aggregates come from the dashboard slice) — the prototype's client-side freeCap()/utilisation
 * math is deliberately NOT reproduced.
 */

/** Mirrors the backend guarantee lifecycle (P9 pledge -> P9 consent -> release/substitution). An unknown value is a contract
 * violation and is REJECTED, never silently rendered. */
export const GUARANTEE_STATUSES = ["pledged", "active", "released"] as const;
export const guaranteeStatusSchema = z.enum(GUARANTEE_STATUSES);
export type GuaranteeStatus = z.infer<typeof guaranteeStatusSchema>;

export const GUARANTEE_STATUS_LABELS: Record<GuaranteeStatus, string> = {
  pledged: "Pledged (unconsented)",
  active: "Active",
  released: "Released",
};

/** GuaranteeOut — the write-response record (the contract exposes NO
 * guarantee list/read endpoint; see the module's honesty notes). */
export const guaranteeSchema = z.object({
  id: z.string(),
  application_id: z.string().nullable(),
  loan_id: z.string().nullable(),
  borrower_member_id: z.string(),
  guarantor_member_id: z.string(),
  /** CANONICAL server money shape (retrofit):
   * `guarantees.amount` is numeric(18,2) CHECK (amount > 0)
   * (migration 0001) via str(Decimal) (_guarantee_out, api/loans.py)
   * — a '-' or garbage shape is a contract violation, REJECTED before
   * it can reach fmtKes in the act dialogs. */
  amount: moneySchema,
  status: guaranteeStatusSchema,
  version: z.number().int(),
});

export type Guarantee = z.infer<typeof guaranteeSchema>;

/** Application stages the P9 contract accepts pledges in — a pledge
 * against approved/disbursed/rejected is refused server-side (409).
 * Kept here (not in the drawer) so the screen's static import does not
 * defeat the drawer-level code splitting. */
export const PLEDGEABLE_STAGES = ["submitted", "appraisal", "committee"] as const;
export type PledgeableStage = (typeof PLEDGEABLE_STAGES)[number];

/** SubstitutionOut — the atomic swap returns BOTH sides; extra
 * keys are STRIPPED at this boundary. */
export const substitutionSchema = z.object({
  released: guaranteeSchema,
  replacement: guaranteeSchema,
});

export type Substitution = z.infer<typeof substitutionSchema>;

// Canonical decimal-string shape (pure string handling, no numeric
// coercion — blocker (a)): up to 2dp and NO leading zeros, so a typed
// "007.10" is rejected at the boundary instead of reaching fmtKes in
// the typed-confirmation banner. A bare "0" (or
// "0.xx") still matches the shape; the non-zero refine below rejects
// the all-zero values.
const DECIMAL_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;
const ALL_ZERO_RE = /^0+(?:\.0{1,2})?$/;
const AMOUNT_SHAPE_MESSAGE = "Enter a positive amount (up to 2 decimal places).";
const AMOUNT_ZERO_MESSAGE = "Enter an amount greater than zero.";

/**
 * Client-side pre-validation of the pledge form (the server re-validates and is the enforcer — least disclosure; capacity is checked under the guarantor's deposit-account row lock server-side). The amount is
 * validated as a decimal STRING shape (2dp, non-zero — pure string
 * inspection, no numeric coercion) and sent as a string: it is never
 * parsed into a float.
 */
export const pledgeCreateSchema = z.object({
  guarantor_member_id: z.string().uuid("Select the guarantor."),
  amount: z
    .string()
    .regex(DECIMAL_RE, AMOUNT_SHAPE_MESSAGE)
    .refine((value) => !ALL_ZERO_RE.test(value), { message: AMOUNT_ZERO_MESSAGE }),
});

export type PledgeCreateInput = z.infer<typeof pledgeCreateSchema>;

/**
 * Client-side pre-validation of the substitution form (atomic swap; contract). The replacement amount is OPTIONAL — when
 * blank the server derives it from the guarantee row (it may only meet
 * or exceed the released amount, enforced server-side).
 * `consent_reference` must cite the attestation evidence — an
 * unreferenced substitute is a server 422. The pre- `consented`
 * boolean is REMOVED from the contract (a caller-asserted consent flag is a rejected design — the lesson): the staff attestation is the
 * act itself plus its cited evidence, never a checkbox.
 */
export const substituteCreateSchema = z.object({
  guarantor_member_id: z.string().uuid("Select the substitute guarantor."),
  consent_reference: z
    .string()
    .trim()
    .min(1, "Cite the consent evidence (e.g. the signed guarantorship form)."),
  amount: z
    .string()
    .regex(DECIMAL_RE, AMOUNT_SHAPE_MESSAGE)
    .refine((value) => !ALL_ZERO_RE.test(value), { message: AMOUNT_ZERO_MESSAGE })
    .nullable(),
});

export type SubstituteCreateInput = z.infer<typeof substituteCreateSchema>;
