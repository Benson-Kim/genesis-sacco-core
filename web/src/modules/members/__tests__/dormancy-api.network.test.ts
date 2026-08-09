/**
 * @jest-environment node
 *
 * Network-layer proofs for the dormancy operations run (#31 ledger
 * (f)) through the REAL generated client + middleware (node
 * environment; fetch stubbed at the network boundary — the reference
 * harness):
 * - POST /members/jobs/dormancy: the body is EXACTLY {batch_size} at
 *   the server's documented default — no as_of, no period, no money
 *   can even be sent (the server resolves both; extra="forbid"
 *   server-side); Idempotency-Key travels as a HEADER only.
 * - The run report round-trips VERBATIM (dates as plain strings,
 *   counts as integers); a garbage count is REJECTED at the boundary.
 * - A 409 (period unconfigured — fail closed) surfaces as a typed
 *   ApiError from EXACTLY ONE request.
 */

// Module scope (the reference harness stays a global script).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";

const calls: FetchCall[] = [];
let runStatus = 200;
let garbageCounts = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const RUN_OUT = {
  as_of: "2026-08-06",
  cutoff: "2025-08-06",
  period_months: 12,
  scanned: 240,
  transitioned: 7,
  batches: 2,
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
  if (path === "/members/jobs/dormancy" && request.method === "POST") {
    if (runStatus !== 200) {
      return json(runStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    if (garbageCounts) {
      return json(200, { ...RUN_OUT, transitioned: "seven" });
    }
    return json(200, RUN_OUT);
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

globalThis.fetch = fetchStub as typeof globalThis.fetch;
process.env.NEXT_PUBLIC_TENANT_ID = TENANT;

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
const membersApi = require("../api") as typeof import("../api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

beforeEach(() => {
  calls.length = 0;
  runStatus = 200;
  garbageCounts = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: "per-tab-refresh" });
});

afterEach(() => {
  session.clearSession();
});

test("POST /members/jobs/dormancy: the body is EXACTLY {batch_size} at the documented default — no as_of, no period, nothing else CAN travel; the key is a HEADER; the report round-trips VERBATIM", async () => {
  const report = await membersApi.runDormancyJob("idem-dormancy-1");

  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe("/members/jobs/dormancy");
  expect(calls[0]!.method).toBe("POST");
  const body = JSON.parse(calls[0]!.body ?? "{}") as Record<string, unknown>;
  // Key-exact: the server's own documented batch-size default and
  // NOTHING else — the period stays server-owned (v1.1 rule 1) and
  // as_of is deliberately unsent (the server resolves today).
  expect(Object.keys(body)).toEqual(["batch_size"]);
  expect(body["batch_size"]).toBe(membersApi.SERVER_DORMANCY_BATCH_SIZE);
  expect(calls[0]!.headers.get("idempotency-key")).toBe("idem-dormancy-1");
  expect(url.href).not.toContain("idem-dormancy-1");
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);

  expect(report).toEqual(RUN_OUT);
});

test("Zod boundary: a garbage count (a string where the contract says integer) is REJECTED, never rendered", async () => {
  garbageCounts = true;
  await expect(membersApi.runDormancyJob("idem-dormancy-2")).rejects.toThrow();
});

test("a 409 (period unconfigured — the run fails CLOSED server-side) surfaces as a typed ApiError from EXACTLY ONE request", async () => {
  runStatus = 409;
  const rejection = await membersApi.runDormancyJob("idem-dormancy-3").catch((e: unknown) => e);
  expect(rejection).toBeInstanceOf(ApiError);
  expect((rejection as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls).toHaveLength(1);
});
