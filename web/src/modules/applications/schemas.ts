import { z } from "zod";
import { moneySchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the loan-applications / committee
 * module (module 3 — P9 API). Shapes mirror the generated client
 * types (components["schemas"]["ApplicationOut"] etc.) — the
 * drift-checked OpenAPI snapshot remains the contract; these schemas
 * only assert it at runtime.
 *
 * MONEY RULE: every monetary field (amount, rate_pct,
 * cover_pct, max_eligible) is a decimal STRING end-to-end. They are
 * rendered via fmtKes or verbatim text and never coerced or combined
 * arithmetically — the server computes every derived figure.
 */

/** Mirrors the backend ApplicationStage enum; the stage drives filters
 * and the workflow indicator, so an unknown value is a contract
 * violation and is REJECTED, never silently rendered (members-module precedent). */
export const APPLICATION_STAGES = [
  "submitted",
  "appraisal",
  "committee",
  "approved",
  "rejected",
  "disbursed",
] as const;

export const applicationStageSchema = z.enum(APPLICATION_STAGES);
export type ApplicationStage = z.infer<typeof applicationStageSchema>;

export const STAGE_LABELS: Record<ApplicationStage, string> = {
  submitted: "Submitted",
  appraisal: "Appraisal",
  committee: "Committee",
  approved: "Approved",
  rejected: "Rejected",
  disbursed: "Disbursed",
};

export const applicationSchema = z.object({
  id: z.string(),
  member_id: z.string(),
  product_id: z.string(),
  /** CANONICAL server money shape (retrofit):
   * `loan_applications.amount` is numeric(18,2) CHECK (amount > 0)
   * (migration 0001) via str(Decimal) (_application_out,
   * api/loans.py) — a '-' or garbage shape is a contract violation,
   * REJECTED before it can reach fmtKes. */
  amount: moneySchema,
  term_months: z.number().int(),
  /** Decimal string — server-resolved from the product;
   * a percentage, never fmtKes-fed, so the shape stays unasserted. */
  rate_pct: z.string(),
  purpose: z.string().nullable(),
  stage: applicationStageSchema,
  /** Decimal string — the server-computed security-cover ratio. */
  cover_pct: z.string(),
  /** Initiator attribution (migration 0036 — the follow-up): the staff principal who created the application, as
   * the BARE user UUID only. Least disclosure: never a
   * name or email — resolving the id stays behind access_control:view
   * server-side, and this client NEVER fetches a directory record for
   * it. NULL is an honest "unattributed" (pre-0036 rows without
   * unambiguous audit history / system-created rows —: an actor
   * is never invented). */
  created_by: z.string().nullable(),
  /** Recommender attribution (close-out, migration 0037):
   * the staff principal who moved the application INTO committee (the
   * "recommend to committee" referral), as the BARE user UUID only —
   * nullable, NOT optional: a missing key is a contract violation,
   * NULL is the honest "not yet referred / unattributed" wire value
   * (pre-0037 rows without unambiguous audit history, system moves —
   * an actor is never invented). Least disclosure: never
   * a name or email; resolving the id stays behind access_control:view
   * server-side, and this client NEVER fetches a directory record for
   * it. The server enforces the SoD keyed on this column regardless:
   * the recommender can neither vote nor disburse (403). */
  recommended_by: z.string().nullable(),
  /** to_cents(deposits x multiplier) added to the two-place guarantee
   * sum (application_max_eligible, loan_applications.py) — Decimal
   * addition keeps the wider scale, so the wire value is ALWAYS the
   * canonical two-place shape; fmtKes-fed (retrofit).
   * Nullable, NOT optional (the hygiene flag):
   * ApplicationOut always serializes the key — a string on the
   * single-application read, NULL on listings (computing it per row would grow queries with result size, scalability) — so a missing
   * key is a contract violation, never a silent pass. */
  max_eligible: moneySchema.nullable(),
  version: z.number().int(),
});

export type Application = z.infer<typeof applicationSchema>;

/** Committee tally after a vote (VoteResultOut) — COUNTS + decision only;
 * the server never discloses who voted which way through this endpoint.
 * Boundary strictness (W58-4): `decision` and `stage` REJECT unknown
 * vocabulary, matching this module's own applicationSchema — an unknown
 * value is a contract violation, never silently rendered. */
export const voteResultSchema = z.object({
  approvals: z.number().int(),
  rejections: z.number().int(),
  decision: z.enum(["approved", "rejected"]).nullable(),
  stage: applicationStageSchema,
});

export type VoteResult = z.infer<typeof voteResultSchema>;

export const VOTES = ["approve", "reject"] as const;
export type Vote = (typeof VOTES)[number];

/** Loan-product reference data (ProductOut) consumed READ-ONLY here for
 * the intake form and product-name display; product administration is
 * the Settings/products module (module 9). Money-ish fields are
 * decimal strings, rendered verbatim. */
export const productSchema = z.object({
  id: z.string(),
  name: z.string(),
  rate_pct: z.string(),
  deposit_multiplier: z.string(),
  max_term_months: z.number().int(),
  guarantors_required: z.number().int(),
  active: z.boolean(),
  version: z.number().int(),
});

export const productsSchema = z.array(productSchema);
export type Product = z.infer<typeof productSchema>;

/** Leading zeros rejected (W58-5, the F6 class): "007.10" is not a
 * well-formed decimal string and would render as "KES 007.10" in the
 * confirmation copy. Pure string shape — no numeric coercion anywhere. */
const DECIMAL_RE = /^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/;
const ALL_ZERO_RE = /^0(?:\.0{1,2})?$/;

/**
 * Client-side pre-validation of the intake form (the server re-validates and is the enforcer — least disclosure). The amount is validated as a decimal
 * STRING shape and sent as a string: it is never parsed into a float.
 * Rate/pricing are NOT here — the API resolves them from the product and
 * rejects caller-sent pricing (extra="forbid").
 */
export const applicationCreateSchema = z.object({
  member_id: z.string().uuid({ message: "Select a member." }),
  product_id: z.string().uuid({ message: "Select a loan product." }),
  amount: z
    .string()
    .regex(DECIMAL_RE, "Enter a positive amount (up to 2 decimal places).")
    .refine((value) => !ALL_ZERO_RE.test(value), {
      message: "Enter an amount greater than zero.",
    }),
  term_months: z
    .number({ invalid_type_error: "Enter the term in months." })
    .int("Enter whole months.")
    .min(1, "Term must be at least 1 month.")
    .max(600, "Term looks too long — check the value."),
  purpose: z.string().max(500, "Keep the purpose under 500 characters.").nullable(),
});

export type ApplicationCreateInput = z.infer<typeof applicationCreateSchema>;
