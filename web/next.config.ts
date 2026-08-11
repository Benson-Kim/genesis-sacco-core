import type { NextConfig } from "next";
import { STATIC_SECURITY_HEADERS } from "./src/lib/security-headers";

/**
 * The CSP (nonce-based, no unsafe-inline scripts) is set per request in
 * src/middleware.ts; the static security headers are ALSO applied here so
 * responses that bypass middleware (static assets) stay covered.
 */
/**
 * Build-time worker cap. Next spawns one build worker per CPU reported
 * by `nproc`, which on shared/containerised hosts is the HOST's core
 * count, not the account's allowance. On a 32-core CloudLinux host that
 * exceeded the per-account LVE process limit and the build died with
 *   Error: spawn /opt/alt/alt-nodejs20/.../node EAGAIN
 * Setting NEXT_BUILD_CPUS=1 there keeps the build inside the cap.
 * Unset everywhere else, so CI and local builds keep Next's default
 * parallelism.
 */
const buildCpus =
  process.env.NEXT_BUILD_CPUS === undefined
    ? undefined
    : Number.parseInt(process.env.NEXT_BUILD_CPUS, 10);

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  ...(buildCpus !== undefined && Number.isFinite(buildCpus)
    ? { experimental: { cpus: buildCpus } }
    : {}),
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
