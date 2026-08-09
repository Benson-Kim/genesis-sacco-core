/**
 * @genesis/api-client — typed client for the Genesis Prestige API.
 *
 * The request/response types in./generated/schema.d.ts are GENERATED from
 * the backend OpenAPI contract (the house doctrine — clients are generated, never hand-written). Regenerate with `npm run generate:api`; CI enforces
 * freshness via the web:spec-drift and web:client-drift jobs.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./generated/schema";

export type { paths, components } from "./generated/schema";
export { ApiError, errorEnvelopeSchema, toApiError } from "./errors";
export type { ErrorEnvelope, FieldError } from "./errors";

export interface GenesisClientOptions {
  baseUrl: string;
  /** Tenant scope — sent as X-Tenant-Id (required pre-auth, harmless after). */
  tenantId?: string;
  /**
   * Returns a valid access token (may refresh first) or null when
   * unauthenticated. Async so callers can single-flight token refresh.
   */
  getAccessToken?: () => Promise<string | null>;
}

export type GenesisClient = ReturnType<typeof createClient<paths>>;

export function createGenesisClient(options: GenesisClientOptions): GenesisClient {
  const client = createClient<paths>({ baseUrl: options.baseUrl });

  const headersMiddleware: Middleware = {
    async onRequest({ request }) {
      if (options.tenantId) {
        request.headers.set("x-tenant-id", options.tenantId);
      }
      if (options.getAccessToken) {
        const token = await options.getAccessToken();
        if (token !== null) {
          request.headers.set("authorization", `Bearer ${token}`);
        }
      }
      return request;
    },
  };

  client.use(headersMiddleware);
  return client;
}

/**
 * Fresh Idempotency-Key for a logical mutation attempt (concurrency safety).
 * Generate ONE key per user intent (e.g. per form submission), reuse it on
 * retries of that same intent, and send it as the `Idempotency-Key` header.
 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/**
 * Per-form key slot backing `idempotencyKeyFor` — hold one in a ref for
 * the lifetime of a form/drawer instance.
 */
export interface IdempotencyKeySlot {
  key: string | null;
  body: string | null;
}

/**
 * Stability/rotation contract for Idempotency-Keys (concurrency safety):
 * retrying the IDENTICAL logical submission (same canonical body) REUSES
 * the key — the server's idempotency store replays the stored response
 * instead of re-executing. Changing the content is a NEW logical intent
 * and rotates the key. One key per intent, never per HTTP attempt.
 * `fresh` is injectable so the contract has a falsifiable test.
 */
export function idempotencyKeyFor(
  slot: IdempotencyKeySlot,
  body: string,
  fresh: () => string = newIdempotencyKey,
): string {
  if (slot.key === null || slot.body !== body) {
    slot.key = fresh();
    slot.body = body;
  }
  return slot.key;
}
