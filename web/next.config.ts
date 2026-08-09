import type { NextConfig } from "next";
import { STATIC_SECURITY_HEADERS } from "./src/lib/security-headers";

/**
 * The CSP (nonce-based, no unsafe-inline scripts) is set per request in
 * src/middleware.ts; the static security headers are ALSO applied here so
 * responses that bypass middleware (static assets) stay covered.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: STATIC_SECURITY_HEADERS.map((header) => ({ ...header })),
      },
    ];
  },
};

export default nextConfig;
