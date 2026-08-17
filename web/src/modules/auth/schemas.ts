import { z } from "zod";

/** Zod-validated response boundary (MASTER_PROMPT §2.3). */
export const tokenResponseSchema = z.object({
  access_token: z.string().min(1),
  refresh_token: z.string().min(1),
  expires_in: z.number().int().positive(),
});

export type TokenResponse = z.infer<typeof tokenResponseSchema>;

export const OTP_LENGTH = 6;

export const otpCodeSchema = z
  .string()
  .regex(/^\d{6}$/, "Enter all 6 digits");

export const emailSchema = z.string().min(3).max(254);

/** Tenant id — a routing UUID entered at the gate, never a credential. */
export const tenantIdSchema = z.string().uuid("Enter a valid tenant id (UUID).");
