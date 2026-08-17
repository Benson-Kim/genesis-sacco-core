import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV === "development";
const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Security headers (scaffold review finding S2 — gate 1.6).
 *
 * - frame-ancestors 'none' + X-Frame-Options DENY: an admin console that
 *   suspends users and assigns roles must never run framed (clickjacking).
 *   The in-app FrameGuard is defence in depth on top of these headers.
 * - CSP: no external script/style/connect origins beyond the API.
 *   `script-src` keeps 'unsafe-inline' because Next.js emits inline
 *   bootstrap/hydration scripts; the nonce-based strict CSP is the P22
 *   deployment hardening item. The PRIMARY XSS control is the absence of
 *   injection sinks (react/no-danger + no-restricted eslint guards and the
 *   hostile-payload tests) — React escapes all interpolated data.
 * - 'unsafe-eval' is added to script-src in development only: Next.js's
 *   React Fast Refresh runtime uses eval() for HMR. It is never present
 *   in production builds.
 */
const scriptSrc = isDev
  ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
  : "script-src 'self' 'unsafe-inline'";

const contentSecurityPolicy = [
  "default-src 'self'",
  scriptSrc,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  `connect-src 'self' ${apiBaseUrl}`,
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  {
    key: "Permissions-Policy",
    value: "camera=(), geolocation=(), microphone=(), payment=()",
  },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
