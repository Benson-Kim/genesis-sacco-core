import { z } from "zod";

/**
 * Zod-validated response boundary for the loan-products API (Settings module — prototype "Loan products" tab). Shapes mirror the
 * generated client types (components["schemas"]["ProductOut"]) — the
 * drift-checked OpenAPI snapshot remains the contract; these schemas
 * only assert it at runtime.
 *
 * MONEY RULE: rate_pct and deposit_multiplier are
 * money-adjacent rule parameters. They travel as API decimal STRINGS,
 * render verbatim, and are SUBMITTED as strings — the client never
 * coerces or computes them.
 */
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

/**
 * Client-side shape checks for the numeric(5,2) rule fields — pure
 * string validation (no numeric conversion); the API remains the
 * enforcer (422 bounds: rate in (0, 100], multiplier > 0).
 */
const DECIMAL_5_2 = /^\d{1,3}(\.\d{1,2})?$/;

/** The mutable rule set (ProductUpdateBody minus version/active). */
export const productRulesFormSchema = z.object({
  rate_pct: z.string().regex(DECIMAL_5_2, "Enter a decimal like 12 or 12.5 (max 2dp)"),
  deposit_multiplier: z.string().regex(DECIMAL_5_2, "Enter a decimal like 3 or 3.5 (max 2dp)"),
  max_term_months: z.coerce
    .number()
    .int("Whole months only")
    .min(1, "At least 1")
    .max(120, "At most 120"),
  guarantors_required: z.coerce
    .number()
    .int("Whole number only")
    .min(0, "At least 0")
    .max(10, "At most 10"),
});

/** Create adds the immutable name (ProductCreateBody). */
export const productCreateFormSchema = productRulesFormSchema.extend({
  name: z.string().min(1, "Enter a product name").max(100, "At most 100 characters"),
});

export type ProductRulesFormValues = z.infer<typeof productRulesFormSchema>;
export type ProductCreateFormValues = z.infer<typeof productCreateFormSchema>;
