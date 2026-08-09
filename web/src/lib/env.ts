/**
 * Client-visible configuration. NEXT_PUBLIC_* values are inlined at build
 * time; secrets NEVER go here (least disclosure — secrets live in CI/CD variables and server-side environments only).
 */
export const env = {
  apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000",
  tenantId: process.env.NEXT_PUBLIC_TENANT_ID ?? "",
} as const;
