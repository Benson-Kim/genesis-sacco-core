import { z } from "zod";

/** Zod-validated response boundary (the house doctrine). */
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

/**
 * /auth/otp/request response boundary. `dev_otp` appears ONLY when the
 * server's fail-closed dev_otp_display flag is on (item 11 — dev-mode tester affordance, REMOVE before staging).
 */
export const otpRequestResponseSchema = z.object({
  status: z.string(),
  dev_otp: z.string().optional(),
});

/**
 * Sign-in identifier blur mirror (item 1): staff sign in with an
 * EMAIL (OtpRequestBody, api/auth.py); the blur check is a courtesy
 * mirror — structural email format on top of the server's 3–254
 * length rule — so the operator corrects immediately. The server
 * stays the truth at the wire.
 */
export const signInEmailSchema = emailSchema.pipe(z.string().email());

/**
 * Sign-in identifier classification (#35 sign-in identifier round).
 * The msisdn VALIDATION mirror is the ONE existing copy in
 * `@/lib/phone` (normalizeKenyaMsisdn) — never duplicated here.
 */
export type SignInIdentifierKind = "email" | "phone";

/**
 * Live as-you-type classification: a value consisting only of digits
 * (with an optional leading +) is being typed as a PHONE; anything
 * else (an '@', letters, punctuation) is being typed as an EMAIL.
 * Empty input classifies as email so the neutral affordances hold.
 */
export function classifyIdentifier(value: string): SignInIdentifierKind {
  const v = value.trim();
  if (v === "") return "email";
  return /^\+?\d+$/.test(v) ? "phone" : "email";
}
