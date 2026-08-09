/**
 * Web-admin security headers (least disclosure — banking posture).
 *
 * - CSP with per-request nonce + strict-dynamic: NO 'unsafe-inline' in
 *   script-src, ever. The nonce is minted in middleware.ts per request and
 *   Next.js propagates it onto its own inline bootstrap scripts (the
 *   documented Next.js nonce pattern; the root layout forces dynamic
 *   rendering so every response is nonced).
 * - Documented Next.js-imposed exception: style-src keeps 'unsafe-inline'
 *   because Next/styled-jsx inject inline <style> elements without nonce
 *   support. Inline STYLES cannot exfiltrate tokens or run script; inline
 *   SCRIPTS (the XSS execution vector) remain nonce-gated.
 * - frame-ancestors 'none' + X-Frame-Options DENY (clickjacking),
 *   Referrer-Policy strict-origin-when-cross-origin, minimal
 *   Permissions-Policy, nosniff.
 *
 * connect-src additionally allows the API origin — the only remote the
 * client ever talks to (no third-party analytics/telemetry/CDNs).
 */
import { env } from "./env";

export interface SecurityHeader {
  key: string;
  value: string;
}

/** Origin of the backend API (the sole permitted remote endpoint). */
export function apiOrigin(): string {
  return new URL(env.apiBaseUrl).origin;
}

export const STATIC_SECURITY_HEADERS: readonly SecurityHeader[] = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value:
      "accelerometer=(), camera=(), geolocation=(), gyroscope=(), " +
      "magnetometer=(), microphone=(), payment=(), usb=()",
  },
] as const;

export function buildContentSecurityPolicy(nonce: string): string {
  return [
    "default-src 'self'",
    // Nonce + strict-dynamic; no 'unsafe-inline', no 'unsafe-eval'.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    // Next.js-imposed exception (styles only) — see module docblock.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    `connect-src 'self' ${apiOrigin()}`,
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");
}
