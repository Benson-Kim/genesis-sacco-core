/**
 * @jest-environment node
 *
 * Network-layer proofs for the legs drill-down API (#31 ledger (g))
 * through the REAL generated client + middleware (node environment;
 * fetch stubbed at the network boundary — the transactions reference
 * harness):
 * - GET only, id as a PATH parameter, NO query parameters (the
 *   contract is unpaginated by construction); bearer/tenant travel as
 *   HEADERS; nothing secret enters a URL.
 * - Legs come back as VERBATIM decimal STRINGS asserted by the Zod
 *   boundary: numbers, garbage shapes ("007.10"), signs and unknown
 *   sides are REJECTED; a widened response (a server-side derived
 *   key) is STRIPPED, never surfaced.
 * - A 404 (unknown/cross-tenant id — 404-before-facts server-side)
 *   surfaces as a typed ApiError from EXACTLY ONE request.
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const TXN_ID = "eeeeeeee-1111-2222-3333-444444444444";
const MISSING_TXN_ID = "eeeeeeee-9999-9999-9999-444444444444";

const calls: FetchCall[] = [];
let legsVariant:
  | "ok"
  | "money-as-number"
  | "garbage-money"
  | "signed-money"
  | "unknown-side"
  | "widened" = "ok";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const LEGS = [
  { account: "cash.mpesa", side: "debit", amount: "100.00" },
  { account: "income.interest", side: "credit", amount: "40.00" },
  { account: "loans.receivable", side: "credit", amount: "60.00" },
];

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
  if (path === `/transactions/${TXN_ID}/legs` && request.method === "GET") {
    switch (legsVariant) {
      case "money-as-number":
        return json(200, { items: [{ ...LEGS[0], amount: 100 }] });
      case "garbage-money":
        // A STRING, so it passes a naive z.string() — the shared
        // moneySchema shape is the only guard before fmtKes.
        return json(200, { items: [{ ...LEGS[0], amount: "007.10" }] });
      case "signed-money":
        // ledger_entries.amount carries CHECK (amount > 0): a sign is
        // a contract violation, REJECTED (never a "negative leg" that
        // would invite client-side netting).
        return json(200, { items: [{ ...LEGS[0], amount: "-100.00" }] });
      case "unknown-side":
        return json(200, { items: [{ ...LEGS[0], side: "contra" }] });
      case "widened":
        // A future server bug adding a derived figure: the boundary
        // STRIPS it — no total can ever reach a render.
        return json(200, { items: LEGS, total: "200.00", net: "0.00" });
      default:
        return json(200, { items: LEGS });
    }
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
const txnApi = require("../api") as typeof import("../api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

beforeEach(() => {
  calls.length = 0;
  legsVariant = "ok";
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: "per-tab-refresh" });
});

afterEach(() => {
  session.clearSession();
});

test("GET /transactions/{id}/legs: id as PATH parameter, NO query parameters (unpaginated by construction); bearer/tenant as HEADERS; rows come back VERBATIM", async () => {
  const legs = await txnApi.fetchTransactionLegs(TXN_ID);

  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe(`/transactions/${TXN_ID}/legs`);
  expect(calls[0]!.method).toBe("GET");
  // Unpaginated by construction: NOTHING rides the query string —
  // no cursor, no limit, no offset, no filter.
  expect([...url.searchParams.keys()]).toEqual([]);
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(url.href).not.toContain("per-tab-refresh");

  // Byte-for-byte the server's strings — never re-encoded.
  expect(legs).toEqual(LEGS);
});

test("Zod boundary: a leg amount as a NUMBER is a contract violation — REJECTED, never rendered (blocker (a))", async () => {
  legsVariant = "money-as-number";
  await expect(txnApi.fetchTransactionLegs(TXN_ID)).rejects.toThrow();
});

test("Zod boundary: a garbage money STRING ('007.10') is rejected by the shared moneySchema shape", async () => {
  legsVariant = "garbage-money";
  await expect(txnApi.fetchTransactionLegs(TXN_ID)).rejects.toThrow();
});

test("Zod boundary: a SIGNED amount is rejected — ledger_entries carries CHECK (amount > 0); a negative leg is a contract violation, not an invitation to net", async () => {
  legsVariant = "signed-money";
  await expect(txnApi.fetchTransactionLegs(TXN_ID)).rejects.toThrow();
});

test("Zod boundary: an unknown side is rejected — the two-value vocabulary is CLOSED", async () => {
  legsVariant = "unknown-side";
  await expect(txnApi.fetchTransactionLegs(TXN_ID)).rejects.toThrow();
});

test("a WIDENED response (server-side total/net keys) is STRIPPED at the boundary — no derived figure can ever reach a render", async () => {
  legsVariant = "widened";
  const legs = await txnApi.fetchTransactionLegs(TXN_ID);
  expect(legs).toEqual(LEGS);
  for (const leg of legs) {
    expect(Object.keys(leg).sort()).toEqual(["account", "amount", "side"]);
  }
});

test("a 404 (unknown/cross-tenant id — 404-before-facts server-side) surfaces as a typed ApiError from EXACTLY ONE request", async () => {
  await expect(txnApi.fetchTransactionLegs(MISSING_TXN_ID)).rejects.toMatchObject({
    status: 404,
  });
  expect(calls).toHaveLength(1);
  const rejection = await txnApi.fetchTransactionLegs(MISSING_TXN_ID).catch((e: unknown) => e);
  expect(rejection).toBeInstanceOf(ApiError);
});
