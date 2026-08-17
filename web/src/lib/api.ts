import { createGenesisClient } from "@genesis/api-client";
import { getActiveTenantId, getValidAccessToken } from "@/modules/auth/session";
import { env } from "@/lib/env";

/**
 * The single authenticated API client for the app. All server state flows
 * through TanStack Query hooks that call this client — no ad-hoc fetch
 * (MASTER_PROMPT §2.3). The tenant is resolved per request from the active
 * session (chosen at the sign-in gate), with the optional NEXT_PUBLIC
 * default only as gate pre-fill — never a hard build-time binding.
 */
export const api = createGenesisClient({
  baseUrl: env.apiBaseUrl,
  getTenantId: getActiveTenantId,
  getAccessToken: getValidAccessToken,
});
