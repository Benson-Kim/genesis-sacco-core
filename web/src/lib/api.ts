import { createGenesisClient } from "@genesis/api-client";
import { getValidAccessToken } from "@/modules/auth/session";
import { env } from "@/lib/env";

/**
 * The single authenticated API client for the app. All server state flows
 * through TanStack Query hooks that call this client — no ad-hoc fetch
 * (the house doctrine).
 */
export const api = createGenesisClient({
  baseUrl: env.apiBaseUrl,
  tenantId: env.tenantId,
  getAccessToken: getValidAccessToken,
});
