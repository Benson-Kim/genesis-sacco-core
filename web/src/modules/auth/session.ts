/**
 * Session store for the auth module.
 *
 * - Access token (JWT, <=15 min) lives in memory only.
 * - Refresh token lives in sessionStorage (cleared when the tab closes);
 *   the backend rotates it on every use with family revocation on reuse.
 * - Refresh is proactive and single-flight: getValidAccessToken() refreshes
 *   before expiry so requests never race a dead token.
 */
import { createGenesisClient } from "@genesis/api-client";
import { tokenResponseSchema } from "./schemas";
import { env } from "@/lib/env";

const REFRESH_TOKEN_KEY = "gp.refresh_token";
/** Refresh when less than this many seconds of validity remain. */
const EXPIRY_MARGIN_SECONDS = 30;

let accessToken: string | null = null;
let ownUserId: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;
const listeners = new Set<() => void>();

/** Bare client for token endpoints only — no auth middleware, no cycles. */
const bareClient = createGenesisClient({
  baseUrl: env.apiBaseUrl,
  tenantId: env.tenantId,
});

function notify(): void {
  listeners.forEach((listener) => listener());
}

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

export function getRefreshToken(): string | null {
  if (!isBrowser()) return null;
  return window.sessionStorage.getItem(REFRESH_TOKEN_KEY);
}

export function setSession(tokens: { accessToken: string; refreshToken: string }): void {
  accessToken = tokens.accessToken;
  const payload = decodeJwtPayload(tokens.accessToken);
  ownUserId = payload !== null && typeof payload.sub === "string" ? payload.sub : null;
  if (isBrowser()) {
    window.sessionStorage.setItem(REFRESH_TOKEN_KEY, tokens.refreshToken);
  }
  notify();
}

export function clearSession(): void {
  accessToken = null;
  ownUserId = null;
  if (isBrowser()) {
    window.sessionStorage.removeItem(REFRESH_TOKEN_KEY);
  }
  notify();
}

/**
 * Decode a JWT payload WITHOUT verifying it. Used solely to read our own
 * `sub` claim for UX (disabling self-role/self-status buttons — the
 * separation-of-duties affordances); the server is the only verifier and
 * enforcer (least disclosure). Salvaged from
 * duo/feature/p13-5-frontend-followthrough @ 198a238.
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[1] === undefined || parts[1] === "") return null;
  try {
    const normalized = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const decoded: unknown = JSON.parse(atob(padded));
    return typeof decoded === "object" && decoded !== null
      ? (decoded as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** The signed-in user's own id (unverified `sub` claim — UX gating only). */
export function getOwnUserId(): string | null {
  return ownUserId;
}

export function hasSession(): boolean {
  return accessToken !== null || getRefreshToken() !== null;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Decode the `exp` claim of a JWT without verifying it (client-side hint only). */
export function tokenExpiresWithin(token: string, marginSeconds: number): boolean {
  try {
    const payload = token.split(".")[1];
    if (payload === undefined) return true;
    const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
    const decoded: unknown = JSON.parse(atob(normalized));
    if (
      typeof decoded !== "object" ||
      decoded === null ||
      typeof (decoded as { exp?: unknown }).exp !== "number"
    ) {
      return true;
    }
    const expiresAt = (decoded as { exp: number }).exp;
    return expiresAt - Date.now() / 1000 < marginSeconds;
  } catch {
    return true;
  }
}

async function refreshSession(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (refreshToken === null) {
    clearSession();
    return null;
  }
  const { data, error } = await bareClient.POST("/auth/refresh", {
    body: { refresh_token: refreshToken },
  });
  if (error !== undefined || data === undefined) {
    clearSession();
    return null;
  }
  const tokens = tokenResponseSchema.parse(data);
  setSession({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token });
  return tokens.access_token;
}

/**
 * Returns a currently-valid access token, refreshing (single-flight) when
 * missing or near expiry. Returns null when unauthenticated.
 */
export async function getValidAccessToken(): Promise<string | null> {
  if (accessToken !== null && !tokenExpiresWithin(accessToken, EXPIRY_MARGIN_SECONDS)) {
    return accessToken;
  }
  if (refreshInFlight === null) {
    refreshInFlight = refreshSession().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}
