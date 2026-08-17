import { z } from "zod";

/**
 * Zod-validated response boundary for the P13.5 users administration API
 * (MASTER_PROMPT §2.3). Shapes mirror the generated client types
 * (components["schemas"]["UserOut"] etc.) — the drift-checked OpenAPI
 * snapshot remains the contract; these schemas only assert it at runtime.
 */
export const userSchema = z.object({
  id: z.string(),
  full_name: z.string(),
  email: z.string(),
  phone: z.string().nullable(),
  branch: z.string().nullable(),
  role_id: z.string(),
  role_name: z.string(),
  status: z.string(),
  last_active_at: z.string().nullable(),
  version: z.number().int(),
});

export type User = z.infer<typeof userSchema>;

export const USER_STATUSES = ["active", "suspended"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

export const roleSchema = z.object({
  id: z.string(),
  name: z.string(),
  is_system: z.boolean(),
});

export const rolesSchema = z.array(roleSchema);
export type Role = z.infer<typeof roleSchema>;

/** Side-effect COUNTS only — never challenge contents (gate 1.6). */
export const otpInvalidateSchema = z.object({
  voided_otp_challenges: z.number().int(),
});

export const otpReenrolSchema = z.object({
  voided_otp_challenges: z.number().int(),
  revoked_refresh_tokens: z.number().int(),
});

export type OtpInvalidateResult = z.infer<typeof otpInvalidateSchema>;
export type OtpReenrolResult = z.infer<typeof otpReenrolSchema>;
