/**
 * @jest-environment node
 *
 * Network-layer proofs for the applications/committee API layer through
 * the REAL generated client + middleware (node environment: real
 * Request/Response/Headers; fetch stubbed at the network boundary),
 * mirroring the settings-api reference harness:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3).
 * - Money travels as decimal STRINGS byte-identically in JSON bodies
 *   (blocker (a)) — never re-encoded as numbers.
 * - Optimistic-lock versions ride in transition bodies; a 409 surfaces
 *   as a typed ApiError from ONE request (no transport-level retry).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const CREATOR_ID = "66666666-6666-6666-6666-666666666666";
const RECOMMENDER_ID = "77777777-7777-7777-7777-777777777777";

const calls: FetchCall[] = [];
let transitionStatus = 200;
let recordGarbageMoney = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const applicationOut = {
  id: APP_ID,
  member_id: MEMBER_ID,
  product_id: PRODUCT_ID,
  amount: "250000.10",
  term_months: 24,
  rate_pct: "12.50",
  purpose: "School fees",
  stage: "submitted",
  cover_pct: "120.00",
  created_by: CREATOR_ID,
  recommended_by: RECOMMENDER_ID,
  max_eligible: "300000.00",
  version: 3,
};

const voteResultOut = { approvals: 2, rejections: 1, decision: null, stage: "committee" };

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
  if (path === "/applications" && request.method === "GET") {
    return json(200, { items: [applicationOut], next_cursor: "cursor-page-2" });
  }
  if (path === "/applications" && request.method === "POST") {
    return json(201, applicationOut);
  }
  if (path === `/applications/${APP_ID}` && request.method === "GET") {
    if (recordGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the shared
      // moneySchema shape (issue #30 A2/S2) is the only thing standing
      // between it and fmtKes on the committee/detail surfaces.
      return json(200, { ...applicationOut, amount: "007.10" });
    }
    return json(200, applicationOut);
  }
  if (path === `/applications/${APP_ID}/transition` && request.method === "POST") {
    if (transitionStatus !== 200) {
      return json(transitionStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    return json(200, { ...applicationOut, stage: "appraisal", version: 4 });
  }
  if (path === `/applications/${APP_ID}/vote` && request.method === "POST") {
    return json(200, voteResultOut);
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
const appsApi = require("../api") as typeof import("../api");
const appSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  transitionStatus = 200;
  recordGarbageMoney = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /applications: keyset params ONLY (stage/cursor/limit — no offset/page); the opaque cursor echoes back VERBATIM", async () => {
  await appsApi.fetchApplicationsPage({ stage: "committee" }, null);
  const page = await appsApi.fetchApplicationsPage({ stage: "committee" }, "cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/applications");
  expect(first.searchParams.get("stage")).toBe("committee");
  expect(first.searchParams.get("limit")).toBe(String(appsApi.APPLICATIONS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  // The boundary transform yields the client-side page shape.
  expect(page.items[0]?.amount).toBe("250000.10");
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("POST /applications: the amount travels as a decimal STRING byte-identically; idempotency as a header", async () => {
  await appsApi.createApplication(
    {
      member_id: MEMBER_ID,
      product_id: PRODUCT_ID,
      amount: "250000.10",
      term_months: 24,
      purpose: null,
    },
    "key-create-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/applications");
  expect(call.headers.get("idempotency-key")).toBe("key-create-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // String on the wire — falsifiable: coerce to Number and the trailing
  // ".10" collapses / the JSON type flips.
  expect(body["amount"]).toBe("250000.10");
  expect(body["term_months"]).toBe(24);
  expect(body["purpose"]).toBeNull();
});

test("POST /applications/{id}/transition: id rides the PATH, version rides the BODY; idempotency as a header", async () => {
  await appsApi.transitionApplication(
    APP_ID,
    { target: "appraisal", version: 3 },
    "key-move-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/applications/${APP_ID}/transition`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-move-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["target"]).toBe("appraisal");
  expect(body["version"]).toBe(3);
});

test("a stale transition surfaces as ONE 409 ApiError — no transport-level retry", async () => {
  transitionStatus = 409;
  const thrown = await appsApi
    .transitionApplication(APP_ID, { target: "committee", version: 2 }, "key-stale-1")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("POST /applications/{id}/vote: the body carries the vote only; the parsed tally is counts + decision", async () => {
  const result = await appsApi.voteOnApplication(APP_ID, "approve", "key-vote-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/applications/${APP_ID}/vote`);
  expect(call.headers.get("idempotency-key")).toBe("key-vote-1");
  expect(JSON.parse(call.body ?? "{}")).toEqual({ vote: "approve" });
  expect(result).toEqual({ approvals: 2, rejections: 1, decision: null, stage: "committee" });
});

test("responses parse through the Zod boundary: record reads return validated shapes", async () => {
  const application = await appsApi.fetchApplication(APP_ID);
  expect(application.amount).toBe("250000.10");
  expect(application.stage).toBe("submitted");
  expect(application.version).toBe(3);
  // Initiator attribution (issue #30 / !66 follow-up): the bare staff
  // UUID travels verbatim through the boundary — least disclosure: no
  // name/email key exists anywhere in the contract or the parse.
  expect(application.created_by).toBe(CREATOR_ID);
  // Recommender attribution (issue #30 close-out, 0037): same treatment.
  expect(application.recommended_by).toBe(RECOMMENDER_ID);
  expect("full_name" in application).toBe(false);
  expect("email" in application).toBe(false);
});

test("recommender accept/reject matrix (issue #30 close-out, 0037): recommended_by is a nullable STRING — the NULL leg is a legitimate 'not referred / unattributed' wire value; a MISSING key is a contract violation", () => {
  const withField = (value: unknown) =>
    appSchemas.applicationSchema.safeParse({ ...applicationOut, recommended_by: value }).success;

  // The two legitimate wire values: the bare staff UUID and the honest
  // NULL (not yet referred to committee, system moves, pre-0037 rows —
  // attribution never invented).
  expect(withField(RECOMMENDER_ID)).toBe(true);
  expect(withField(null)).toBe(true);

  // Shape drift REJECTED: attribution never arrives as a number, an
  // object (a smuggled name/email payload) or a boolean.
  expect(withField(42)).toBe(false);
  expect(withField({ id: RECOMMENDER_ID, full_name: "Jane" })).toBe(false);
  expect(withField(true)).toBe(false);

  // KEY EXACTNESS: dropping the key entirely is a contract violation —
  // the field is nullable, not optional (falsifiable: soften the schema
  // to .optional() and this leg fails).
  const missing: Record<string, unknown> = { ...applicationOut };
  delete missing["recommended_by"];
  expect(appSchemas.applicationSchema.safeParse(missing).success).toBe(false);
});

test("attribution accept/reject matrix (issue #30 / !66 follow-up): created_by is a nullable STRING — the NULL leg is a legitimate 'unattributed' wire value; a MISSING key is a contract violation", () => {
  const withField = (value: unknown) =>
    appSchemas.applicationSchema.safeParse({ ...applicationOut, created_by: value }).success;

  // The two legitimate wire values: the bare staff UUID and the honest
  // NULL (pre-0036 rows / system-created — attribution never invented).
  expect(withField(CREATOR_ID)).toBe(true);
  expect(withField(null)).toBe(true);

  // Shape drift REJECTED: attribution never arrives as a number, an
  // object (a smuggled name/email payload) or a boolean.
  expect(withField(42)).toBe(false);
  expect(withField({ id: CREATOR_ID, full_name: "Jane" })).toBe(false);
  expect(withField(true)).toBe(false);

  // KEY EXACTNESS: dropping the key entirely is a contract violation —
  // the field is nullable, not optional (falsifiable: soften the schema
  // to .optional() and this leg fails).
  const missing: Record<string, unknown> = { ...applicationOut };
  delete missing["created_by"];
  expect(appSchemas.applicationSchema.safeParse(missing).success).toBe(false);
});

test("a garbage money STRING is REJECTED at the wire boundary (issue #30 A2/S2) — a value that passes z.string() can still never reach fmtKes", async () => {
  recordGarbageMoney = true;
  const thrown = await appsApi.fetchApplication(APP_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("amount");
});

test("issue #30 A2/S2 accept/reject matrix: amount/max_eligible assert the CANONICAL server shape; the percentages stay contract-typed strings", () => {
  const withField = (field: string, value: unknown) =>
    appSchemas.applicationSchema.safeParse({ ...applicationOut, [field]: value }).success;

  // Canonical shapes ACCEPTED: loan_applications.amount is
  // numeric(18,2) CHECK (amount > 0) via str(Decimal);
  // max_eligible = to_cents(deposits x multiplier) added to the
  // two-place guarantee sum — Decimal addition keeps the wider scale
  // (hand-computed oracles).
  expect(withField("amount", "250000.10")).toBe(true);
  expect(withField("max_eligible", "300000.00")).toBe(true);
  expect(withField("max_eligible", null)).toBe(true);

  // Garbage shapes REJECTED on both money fields — each previously
  // flowed into fmtKes unchallenged (bare z.string()).
  for (const value of ["abc", "1e5", "007.10", "250000.1", "250000.100", "250000", "250,000.10", " 250000.10", "NaN", ""]) {
    expect(withField("amount", value)).toBe(false);
    expect(withField("max_eligible", value)).toBe(false);
  }
  // CHECK (amount > 0) / non-negative composition: a '-' is a
  // contract violation on both.
  expect(withField("amount", "-1.00")).toBe(false);
  expect(withField("max_eligible", "-1.00")).toBe(false);

  // KEY EXACTNESS (the !70 hygiene flag, #31 batch 11): dropping the
  // max_eligible key entirely is a contract violation — ApplicationOut
  // always serializes it (string on the single read, null on listings),
  // so the field is nullable, NOT optional (falsifiable: soften the
  // schema back to .optional() and this leg fails).
  const missing: Record<string, unknown> = { ...applicationOut };
  delete missing["max_eligible"];
  expect(appSchemas.applicationSchema.safeParse(missing).success).toBe(false);
});
