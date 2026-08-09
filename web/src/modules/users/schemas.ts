import { z } from "zod";
import type { components } from "@genesis/api-client";
import { isoTimestampSchema } from "@/lib/schemas";

/**
 * Zod-validated response boundary for the users administration API (salvaged from duo/feature/p13-5-frontend-followthrough @ 198a238).
 * Shapes mirror the generated client types (the house doctrine)
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
  // Boundary policy verified against the generated contract (W56-2):
  // UserOut.status is contract-typed as plain `string` (only the WRITE
  // body's UserStatusBody carries the UserStatus enum), so rejecting an
  // unknown value here would claim MORE than the contract and break on
  // contract-conforming responses. This is the one sanctioned inert-text
  // fallback: unknown statuses render as inert TEXT (StatusPill) and are
  // never a styled affordance. The known vocabulary is compile-time
  // pinned to the generated enum below, so it can never silently drift.
  status: z.string(),
  /** `datetime.isoformat()` or null (api/users.py — serialised only
   * when the column is set) — feeds fmtDateTime (UserDetailDrawer,
   * UsersScreen title) and relTime (UsersScreen row): a garbage value
   * is REJECTED at the boundary, never rendered "Invalid Date"
   * (retrofit). */
  last_active_at: isoTimestampSchema.nullable(),
  version: z.number().int(),
});

export type User = z.infer<typeof userSchema>;

export const USER_STATUSES = ["active", "suspended"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

/** Bidirectional type equality — `true` only when A and B are the same
 * union (used as a compile-time drift tripwire for mirrored enums). */
type MirrorEquals<A, B> = [A] extends [B] ? ([B] extends [A] ? true : never) : never;

/** Compile-time drift tripwire (W56-2): if the generated contract's
 * UserStatus enum ever gains or loses a value, this declaration stops
 * type-checking — the mirrored vocabulary cannot silently diverge. */
export const USER_STATUS_MIRROR_PINNED: MirrorEquals<
  UserStatus,
  components["schemas"]["UserStatus"]
> = true;

export const roleSchema = z.object({
  id: z.string(),
  name: z.string(),
  is_system: z.boolean(),
});

export const rolesSchema = z.array(roleSchema);
export type Role = z.infer<typeof roleSchema>;

/** Side-effect COUNTS only — never challenge contents (least disclosure). */
export const otpInvalidateSchema = z.object({
  voided_otp_challenges: z.number().int(),
});

export const otpReenrolSchema = z.object({
  voided_otp_challenges: z.number().int(),
  revoked_refresh_tokens: z.number().int(),
});

export type OtpInvalidateResult = z.infer<typeof otpInvalidateSchema>;
export type OtpReenrolResult = z.infer<typeof otpReenrolSchema>;
