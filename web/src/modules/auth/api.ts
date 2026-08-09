/**
 * Auth flows against the generated client, mirroring the prototype OTP gate
 * (request -> 6-digit verify) and the backend contract (P3).
 */
import { newIdempotencyKey, toApiError } from "@genesis/api-client";
import { otpRequestResponseSchema, tokenResponseSchema, type TokenResponse } from "./schemas";
import { clearSession, getRefreshToken, setSession } from "./session";
import { clearSessionScopedStores } from "./sessionScopedStores";
import { api } from "@/lib/api";

export async function requestOtp(identifier: string): Promise<{ devOtp: string | null }> {
  // #35 sign-in identifier: the unified field carries an email OR a
  // normalized Kenya msisdn; the server classifies with its one rule.
  const { data, error, response } = await api.POST("/auth/otp/request", {
    body: { identifier },
  });
  if (error !== undefined) {
    throw toApiError(error, response);
  }
  // DEV-ONLY (item 11, REMOVE BEFORE STAGING): the server includes
  // dev_otp only behind its fail-closed flag; absent means null here —
  // the note simply does not render.
  const parsed = otpRequestResponseSchema.safeParse(data);
  return { devOtp: parsed.success ? (parsed.data.dev_otp ?? null) : null };
}

export async function verifyOtp(input: {
  identifier: string;
  code: string;
  /** One key per logical submission; reuse on retry of the same attempt (concurrency safety). */
  idempotencyKey?: string;
}): Promise<TokenResponse> {
  const { data, error, response } = await api.POST("/auth/otp/verify", {
    body: { identifier: input.identifier, code: input.code },
    headers: { "Idempotency-Key": input.idempotencyKey ?? newIdempotencyKey() },
  });
  if (error !== undefined || data === undefined) {
    throw toApiError(error, response);
  }
  const tokens = tokenResponseSchema.parse(data);
  setSession({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token });
  return tokens;
}

export async function logout(): Promise<void> {
  const refreshToken = getRefreshToken();
  try {
    if (refreshToken !== null) {
      await api.POST("/auth/logout", { body: { refresh_token: refreshToken } });
    }
  } finally {
    // Local sign-out must never be blocked by a failed revocation call.
    clearSession();
    // Every per-tab witnessed registry dies with the session (W58-2,
    // the F2 class): the next operator inherits no witnessed
    // attributions and no armed affordances.
    clearSessionScopedStores();
  }
}
