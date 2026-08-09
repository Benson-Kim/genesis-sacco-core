/**
 * @jest-environment node
 *
 * Network-layer proofs for the settings + products API layers through
 * the REAL generated client + middleware (node environment: real
 * Request/Response/Headers; fetch stubbed at the network boundary),
 * mirroring the users-api reference harness:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Money parameters travel as decimal STRINGS byte-identically in
 *   JSON bodies (blocker (a)) — never re-encoded as numbers.
 * - Optimistic-lock versions ride in bodies; a 409 surfaces as a typed
 *   ApiError from ONE request (no transport-level retry).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";

const calls: FetchCall[] = [];
let settingsPutStatus = 200;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const settingsOut = {
  configured: true,
  version: 5,
  corrupt_keys: [],
  deposit_interest_annual_rate_pct: "9.00",
  dividend_rate_pct: "14.00",
  deposit_rebate_rate_pct: null,
  penalty_rate_pct_per_month: "1.50",
  penalty_grace_days: 5,
  penalty_charged_on: "instalment_in_arrears",
  loan_interest_method: "reducing_balance",
  loan_interest_basis: "actual_365",
  loan_rate_bands: null,
  min_share_capital: "15000.00",
  registration_fee: "1000.00",
  min_monthly_contribution: "2000.00",
  max_member_exposure: "2000000.00",
  dormancy_period_months: 6,
  financial_year_end_month: 12,
  exit_notice_period_days: 30,
  exit_fee: "500.00",
  committee_size: 5,
  committee_quorum: 3,
  approval_bands: null,
};

const productOut = {
  id: PRODUCT_ID,
  name: "Development Loan",
  rate_pct: "12.50",
  deposit_multiplier: "3.00",
  max_term_months: 24,
  guarantors_required: 2,
  active: true,
  version: 1,
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function fetchStub(input: Request | string | URL, init?: RequestInit): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input, init);
  const body = request.method === "GET" ? null : await request.clone().text();
  calls.push({ url: request.url, method: request.method, headers: request.headers, body });
  const path = new URL(request.url).pathname;
  if (request.headers.get("authorization") === null) {
    return json(401, { category: "unauthenticated", correlation_id: "corr-a" });
  }
  if (path === "/settings" && request.method === "GET") {
    return json(200, settingsOut);
  }
  if (path === "/settings" && request.method === "PUT") {
    if (settingsPutStatus !== 200) {
      return json(settingsPutStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    return json(200, { ...settingsOut, version: 6 });
  }
  if (path === "/products" && request.method === "GET") {
    return json(200, [productOut]);
  }
  if (path === "/products" && request.method === "POST") {
    return json(201, productOut);
  }
  if (path === `/products/${PRODUCT_ID}` && request.method === "PUT") {
    return json(200, { ...productOut, version: 2 });
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

// The generated client captures globalThis.fetch at creation time, so the
// stub must be installed BEFORE the modules under test are imported.
globalThis.fetch = fetchStub as typeof globalThis.fetch;

// Tenant scope is build-time configuration (src/lib/env reads
// NEXT_PUBLIC_TENANT_ID at module load) — set BEFORE requiring modules.
process.env.NEXT_PUBLIC_TENANT_ID = TENANT;

// Node has no window: back the single per-tab custody site with a Map
// (the real storage read/write path still runs) — reference-harness model.
const tabStorage = new Map<string, string>();
(globalThis as { window?: unknown }).window = {
  sessionStorage: {
    getItem: (key: string): string | null => tabStorage.get(key) ?? null,
    setItem: (key: string, value: string): void => {
      tabStorage.set(key, String(value));
    },
    removeItem: (key: string): void => {
      tabStorage.delete(key);
    },
    clear: (): void => {
      tabStorage.clear();
    },
    get length(): number {
      return tabStorage.size;
    },
  },
};

/* eslint-disable @typescript-eslint/no-require-imports */
const session = require("@/modules/auth/session") as typeof import("@/modules/auth/session");
const settingsApi = require("../api") as typeof import("../api");
const productsApi = require("@/modules/products/api") as typeof import("@/modules/products/api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  settingsPutStatus = 200;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("PUT /settings: bearer + tenant + idempotency as headers; version in the body; nothing secret in the URL", async () => {
  await settingsApi.updateSettings(
    { version: 5, dividend_rate_pct: "14.50", deposit_rebate_rate_pct: null },
    "key-settings-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("PUT");
  expect(new URL(call.url).pathname).toBe("/settings");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.headers.get("idempotency-key")).toBe("key-settings-1");
  expect(call.url).not.toContain(REFRESH_VALUE);
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["version"]).toBe(5);
  // Decimal strings travel byte-identically — string type on the wire,
  // an explicit null clears (falsifiable: coerce to Number and the
  // trailing zero disappears / the type flips).
  expect(body["dividend_rate_pct"]).toBe("14.50");
  expect(body["deposit_rebate_rate_pct"]).toBeNull();
});

test("products mutations: rule decimals stay STRINGS on the wire; ids ride the path; idempotency as a header", async () => {
  await productsApi.createProduct(
    {
      name: "Development Loan",
      rate_pct: "12.50",
      deposit_multiplier: "3.00",
      max_term_months: 24,
      guarantors_required: 2,
    },
    "key-create-1",
  );
  await productsApi.updateProduct(
    PRODUCT_ID,
    { version: 1, rate_pct: "13.00" },
    "key-update-1",
  );
  expect(calls).toHaveLength(2);

  const create = calls[0]!;
  expect(create.method).toBe("POST");
  expect(new URL(create.url).pathname).toBe("/products");
  expect(create.headers.get("idempotency-key")).toBe("key-create-1");
  const createBody = JSON.parse(create.body ?? "{}") as Record<string, unknown>;
  expect(createBody["rate_pct"]).toBe("12.50");
  expect(createBody["deposit_multiplier"]).toBe("3.00");

  const update = calls[1]!;
  expect(update.method).toBe("PUT");
  expect(new URL(update.url).pathname).toBe(`/products/${PRODUCT_ID}`);
  expect(new URL(update.url).search).toBe("");
  expect(update.headers.get("idempotency-key")).toBe("key-update-1");
  const updateBody = JSON.parse(update.body ?? "{}") as Record<string, unknown>;
  expect(updateBody["version"]).toBe(1);
  expect(updateBody["rate_pct"]).toBe("13.00");
});

test("a stale settings write surfaces as ONE 409 ApiError — no transport-level retry", async () => {
  settingsPutStatus = 409;
  const thrown = await settingsApi
    .updateSettings({ version: 4, dividend_rate_pct: "14.50" }, "key-stale-1")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});

test("responses parse through the Zod boundary: list + record reads return validated shapes", async () => {
  const products = await productsApi.fetchProducts();
  expect(products).toHaveLength(1);
  expect(products[0]?.rate_pct).toBe("12.50");
  const settings = await settingsApi.fetchSettings();
  expect(settings.version).toBe(5);
  expect(settings.min_share_capital).toBe("15000.00");
});
