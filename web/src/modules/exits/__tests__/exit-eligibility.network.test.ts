/**
 * @jest-environment node
 *
 * U6 advisory exit-eligibility — NETWORK leg through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the exits reference harness:
 * - the member id rides the contract's PATH parameter; the query
 *   string is EMPTY; bearer/tenant travel as HEADERS; the refresh
 *   secret never enters a URL; a GET carries NO Idempotency-Key.
 * - exactly ONE request per call (wire-count assertion).
 * - the Zod boundary is KEY-EXACT: every field is REQUIRED and none is
 *   nullable — a missing key, a null, a float/negative/stringified
 *   count, a stringified boolean, an unknown member_status or a
 *   garbage as_of is a contract violation and is REJECTED; extra keys
 *   are STRIPPED.
 * - the contract carries NO amount: the parsed key set is asserted
 *   exactly, and no money-named key exists in it (falsifiable: add an
 *   amount field to the schema and this fails).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";

const calls: FetchCall[] = [];
let violateFloatCount = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const ELIGIBILITY_OUT = {
  member_id: MEMBER_ID,
  member_status: "active",
  status_allows_exit: true,
  open_exit_exists: false,
  live_guarantees_count: 1,
  open_applications_count: 0,
  active_loans_count: 2,
  unresolved_writeoff_claim: false,
  advisory_eligible: false,
  as_of: "2026-08-06T12:00:00+00:00",
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
  if (path === `/member-exits/eligibility/${MEMBER_ID}` && request.method === "GET") {
    if (violateFloatCount) {
      return json(200, { ...ELIGIBILITY_OUT, live_guarantees_count: 1.5 });
    }
    return json(200, { ...ELIGIBILITY_OUT, internal_blocker_sql: "SQL-SECRET" });
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
const exitsApi = require("../api") as typeof import("../api");
const exitSchemas = require("../schemas") as typeof import("../schemas");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  violateFloatCount = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET eligibility: the member id is a PATH parameter, the query string is EMPTY, auth/tenant are HEADERS (no Idempotency-Key on a read), the refresh secret never enters the URL — exactly ONE request", async () => {
  const facts = await exitsApi.fetchExitEligibility(MEMBER_ID);

  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("GET");
  const url = new URL(call.url);
  expect(url.pathname).toBe(`/member-exits/eligibility/${MEMBER_ID}`);
  expect(url.search).toBe("");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.headers.get("idempotency-key")).toBeNull();
  expect(call.url).not.toContain(REFRESH_VALUE);
  expect(call.body).toBeNull();

  // The facts parse VERBATIM — the server's own advisory conjunction
  // survives even when the component rows look eligible (it is NEVER
  // re-derived client-side), and counts stay integers.
  expect(facts.advisory_eligible).toBe(false);
  expect(facts.live_guarantees_count).toBe(1);
  expect(facts.active_loans_count).toBe(2);
  expect(facts.as_of).toBe("2026-08-06T12:00:00+00:00");
});

test("the contract carries NO amount: the parsed key set is EXACT, extra keys are STRIPPED, and no money-named key exists (falsifiable: widen the schema and this fails)", async () => {
  const facts = await exitsApi.fetchExitEligibility(MEMBER_ID);

  const keys = Object.keys(facts).sort();
  expect(keys).toEqual(
    [
      "active_loans_count",
      "advisory_eligible",
      "as_of",
      "live_guarantees_count",
      "member_id",
      "member_status",
      "open_applications_count",
      "open_exit_exists",
      "status_allows_exit",
      "unresolved_writeoff_claim",
    ].sort(),
  );
  // The stubbed internal field can never reach a screen.
  expect("internal_blocker_sql" in facts).toBe(false);
  // Least disclosure (gate 1.6): nothing money-shaped travels here.
  for (const key of keys) {
    expect(key).not.toMatch(/amount|payable|fee|balance|equity|shares|deposit/);
  }
});

test("KEY-EXACT accept/reject matrix: every field is REQUIRED and NOT nullable; wrong shapes are REJECTED (missing key, null, float/negative/string counts, string booleans, unknown status, garbage timestamp)", () => {
  const parse = (value: unknown) => exitSchemas.exitEligibilitySchema.safeParse(value).success;

  // The canonical wire value is ACCEPTED.
  expect(parse(ELIGIBILITY_OUT)).toBe(true);

  // KEY EXACTNESS: dropping ANY key is a contract violation — nothing
  // here is optional (falsifiable: soften one field and this fails).
  for (const key of Object.keys(ELIGIBILITY_OUT)) {
    const missing: Record<string, unknown> = { ...ELIGIBILITY_OUT };
    delete missing[key];
    expect(parse(missing)).toBe(false);
  }

  // NOT NULLABLE: null is a contract violation on every field.
  for (const key of Object.keys(ELIGIBILITY_OUT)) {
    expect(parse({ ...ELIGIBILITY_OUT, [key]: null })).toBe(false);
  }

  // Counts: non-negative INTEGERS only.
  for (const key of [
    "live_guarantees_count",
    "open_applications_count",
    "active_loans_count",
  ]) {
    expect(parse({ ...ELIGIBILITY_OUT, [key]: 1.5 })).toBe(false);
    expect(parse({ ...ELIGIBILITY_OUT, [key]: -1 })).toBe(false);
    expect(parse({ ...ELIGIBILITY_OUT, [key]: "2" })).toBe(false);
    expect(parse({ ...ELIGIBILITY_OUT, [key]: 0 })).toBe(true);
  }

  // Booleans: never stringified.
  for (const key of [
    "status_allows_exit",
    "open_exit_exists",
    "unresolved_writeoff_claim",
    "advisory_eligible",
  ]) {
    expect(parse({ ...ELIGIBILITY_OUT, [key]: "true" })).toBe(false);
    expect(parse({ ...ELIGIBILITY_OUT, [key]: 1 })).toBe(false);
  }

  // member_status rejects unknown vocabulary (W58-4 posture): an
  // unrecognised status must never label a checklist row.
  expect(parse({ ...ELIGIBILITY_OUT, member_status: "suspended" })).toBe(false);
  for (const status of ["active", "arrears", "dormant", "exited"]) {
    expect(parse({ ...ELIGIBILITY_OUT, member_status: status })).toBe(true);
  }

  // as_of asserts the timestamp SHAPE (the !63 F-R4 lesson).
  expect(parse({ ...ELIGIBILITY_OUT, as_of: "yesterday-ish" })).toBe(false);
});

test("a contract-violating response is REJECTED at the WIRE boundary — a float count can never reach the checklist (one request, no retry at this layer)", async () => {
  // Flag consumed INSIDE the pre-installed stub (the generated client
  // captured globalThis.fetch at creation time — reassigning it later
  // would be a no-op; the reference-harness flag model applies).
  violateFloatCount = true;
  const thrown = await exitsApi.fetchExitEligibility(MEMBER_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(String(thrown)).toContain("live_guarantees_count");
  expect(calls).toHaveLength(1);
});
