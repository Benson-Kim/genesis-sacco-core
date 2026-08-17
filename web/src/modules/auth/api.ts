/**
 * Auth flows against the generated client, mirroring the prototype OTP gate
 * (request -> 6-digit verify) and the backend contract (P3).
 */
import { newIdempotencyKey, toApiError } from "@genesis/api-client";
import { tokenResponseSchema, type TokenResponse } from "./schemas";
import { clearSession, getRefreshToken, setSession } from "./session";
import { api } from "@/lib/api";

export async function requestOtp(email: string): Promise<void> {
  const { error, response } = await api.POST("/auth/otp/request", {
    body: { email },
  });
  if (error !== undefined) {
    throw toApiError(error, response);
  }
}

export async function verifyOtp(input: {
  email: string;
  code: string;
  /** One key per logical submission; reuse on retry of the same attempt (gate 1.4). */
  idempotencyKey?: string;
}): Promise<TokenResponse> {
  const { data, error, response } = await api.POST("/auth/otp/verify", {
    body: { email: input.email, code: input.code },
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
  }
}
