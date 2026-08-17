/**
 * Session store for the auth module.
 *
 * Token custody (P3 transport pattern, gate 1.6 — scaffold review finding S1):
 * - BOTH tokens live in JS module memory only. No cookie, no local or
 *   session storage: any script-readable persistent storage turns a
 *   one-shot XSS into durable session theft. A page reload therefore
 *   requires re-login — the accepted trade-off for a core-banking console
 *   (carried forward from the !25 threat model).
 * - The refresh token travels ONLY in POST bodies; the access token ONLY in
 *   the Authorization header. Neither may appear in a URL or in storage
 *   (guarded by eslint no-restricted rules and the token-custody tests).
 * - Refresh is proactive and single-flight: getValidAccessToken() refreshes
 *   before expiry so requests never race a dead token. The backend rotates
 *   the refresh token on every use with family revocation on reuse.
 *
 * The only persisted value is the tenant id — routing configuration, not a
 * credential (this file is the single allowlisted storage site; see
 * eslint.config.mjs).
 */
import { createGenesisClient } from "@genesis/api-client";
import { tokenResponseSchema } from "./schemas";
import { env } from "@/lib/env";

/** Refresh when less than this many seconds of validity remain. */
const EXPIRY_MARGIN_SECONDS = 30;
/** Persisted tenant id (a UUID naming the tenant — not a secret). */
const TENANT_STORAGE_KEY = "gp.tenant";

let accessToken: string | null = null;
let refreshToken: string | null = null;
let tenantId: string | null = null;
let ownUserId: string | null = null;
let refreshInFlight: Promise<string | null> | null = null;
const listeners = new Set<() => void>();

/** Bare client for token endpoints only — no auth middleware, no cycles. */
const bareClient = createGenesisClient({
  baseUrl: env.apiBaseUrl,
  getTenantId: () => tenantId,
});

function notify(): void {
  listeners.forEach((listener) => listener());
}

function isBrowser(): boolean {
  return typeof window !== "undefined";
}

/**
 * Decode a JWT payload WITHOUT verifying it. Used solely to read our own
 * `sub` claim for UX (disabling self-role/self-status buttons); the server
 * is the only verifier and enforcer (gate 1.6).
 */
export function decodeJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[1] === undefined) return null;
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

export function getRefreshToken(): string | null {
  return refreshToken;
}

export function setSession(tokens: { accessToken: string; refreshToken: string }): void {
  accessToken = tokens.accessToken;
  refreshToken = tokens.refreshToken;
  const payload = decodeJwtPayload(tokens.accessToken);
  ownUserId = payload !== null && typeof payload.sub === "string" ? payload.sub : null;
  notify();
}

export function clearSession(): void {
  accessToken = null;
  refreshToken = null;
  ownUserId = null;
  notify();
}

export function hasSession(): boolean {
  return accessToken !== null || refreshToken !== null;
}

/** The signed-in user's own id (unverified `sub` claim — UX gating only). */
export function getOwnUserId(): string | null {
  return ownUserId;
}

/** Active tenant for this session (sent as X-Tenant-Id by the client). */
export function getActiveTenantId(): string | null {
  return tenantId;
}

export function setActiveTenantId(value: string): void {
  tenantId = value;
}

/**
 * Tenant id persistence — non-secret routing config. Storage may be
 * unavailable (private mode); the gate then simply asks again.
 */
export function getStoredTenantId(): string {
  if (!isBrowser()) return "";
  try {
    return window.localStorage.getItem(TENANT_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function storeTenantId(value: string): void {
  if (!isBrowser()) return;
  try {
    window.localStorage.setItem(TENANT_STORAGE_KEY, value);
  } catch {
    // Non-fatal: convenience only.
  }
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Decode the `exp` claim of a JWT without verifying it (client-side hint only). */
export function tokenExpiresWithin(token: string, marginSeconds: number): boolean {
  const payload = decodeJwtPayload(token);
  if (payload === null || typeof payload.exp !== "number") return true;
  return payload.exp - Date.now() / 1000 < marginSeconds;
}

async function refreshSession(): Promise<string | null> {
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
  if (refreshToken === null) {
    return null;
  }
  if (refreshInFlight === null) {
    refreshInFlight = refreshSession().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}
