/**
 * @jest-environment node
 *
 * Network-layer proofs for the branches registry API layer through
 * the REAL generated client + middleware (node environment: real
 * Request/Response/Headers; fetch stubbed at the network boundary),
 * mirroring the exits/dividends reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3)
 *   and NO filter parameter exists at all (the P13.6 list contract
 *   declares none).
 * - Every wire body is asserted KEY-BY-KEY: create {name}, rename
 *   {version, name}, each assignment {version} (the USER/MEMBER row's
 *   pinned version — nothing else can even be sent; extra="forbid"
 *   server-side), backfill {batch_size: 200} (the SERVER's own
 *   documented default, never an operator choice). Write query
 *   strings are EMPTY. NO money parameter exists anywhere (the
 *   router: branch is organisational metadata only).
 * - Assignment ids travel as PATH parameters serialized by the
 *   generated client — asserted against the exact route.
 * - Boundary accept/reject matrix: garbage timestamps REJECTED,
 *   integers never numeric strings, nullable-free key exactness
 *   (every BranchOut key is REQUIRED). NO money field exists on this
 *   contract — the matrix asserts the shapes it ACTUALLY carries.
 * - A 409 (duplicate name / stale version) and a 422 each surface as
 *   a typed ApiError from ONE request (no transport-level retry).
 */

export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";
const TARGET_USER_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const MEMBER_ID = "cccccccc-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let createStatus = 201;
let updateStatus = 200;
let assignStatus = 200;
let listGarbageTimestamp = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const branchOut = {
  id: BRANCH_ID,
  name: "Nairobi West",
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

const assignmentOut = {
  entity_id: TARGET_USER_ID,
  branch_id: BRANCH_ID,
  branch_name: "Nairobi West",
  version: 4,
};

const backfillOut = {
  scanned: 12,
  branches_created: 3,
  users_linked: 12,
  batches: 1,
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
  if (path === "/branches" && request.method === "GET") {
    if (listGarbageTimestamp) {
      return json(200, {
        items: [{ ...branchOut, created_at: "yesterday-ish" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [branchOut], next_cursor: "cursor-page-2" });
  }
  if (path === "/branches" && request.method === "POST") {
    if (createStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-dup" });
    }
    if (createStatus === 422) {
      return json(422, {
        category: "validation",
        correlation_id: "corr-v",
        fields: { name: "String should have at least 1 character" },
      });
    }
    return json(201, { ...branchOut, internal_registry_note: "REG-SECRET" });
  }
  if (path === `/branches/${BRANCH_ID}` && request.method === "GET") {
    return json(200, branchOut);
  }
  if (path === `/branches/${BRANCH_ID}` && request.method === "PUT") {
    if (updateStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-stale" });
    }
    return json(200, { ...branchOut, name: "Nairobi West HQ", version: 2 });
  }
  if (path === `/branches/${BRANCH_ID}/users/${TARGET_USER_ID}` && request.method === "PUT") {
    if (assignStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-user-stale" });
    }
    return json(200, assignmentOut);
  }
  if (path === `/branches/${BRANCH_ID}/members/${MEMBER_ID}` && request.method === "PUT") {
    return json(200, { ...assignmentOut, entity_id: MEMBER_ID, version: 7 });
  }
  if (path === "/jobs/branch-backfill" && request.method === "POST") {
    return json(200, { ...backfillOut, internal_scan_trace: "TRACE-SECRET" });
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
const branchesApi = require("../api") as typeof import("../api");
const branchSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  createStatus = 201;
  updateStatus = 200;
  assignStatus = 200;
  listGarbageTimestamp = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /branches: keyset params ONLY (no offset/page) and NO filter parameter at all; the opaque cursor echoes back VERBATIM; secrets ride as HEADERS", async () => {
  await branchesApi.fetchBranchesPage(null);
  const page = await branchesApi.fetchBranchesPage("cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/branches");
  expect(first.searchParams.get("limit")).toBe(String(branchesApi.BRANCHES_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  // The P13.6 list contract declares NO filters — none are sent.
  expect([...first.searchParams.keys()].sort()).toEqual(["limit"]);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  expect(page.items[0]?.name).toBe("Nairobi West");
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("GET /branches/{id}: the id travels as a PATH parameter; the record parses verbatim", async () => {
  const record = await branchesApi.fetchBranch(BRANCH_ID);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/branches/${BRANCH_ID}`);
  expect(new URL(call.url).search).toBe("");
  expect(record.version).toBe(1);
  expect(record.created_at).toBe("2026-08-01T09:00:00+00:00");
});

test("POST /branches: the body is exactly {name} — no money, cost or any other field can even be sent; the Idempotency-Key rides as a HEADER; EXTRA response keys are STRIPPED", async () => {
  const record = await branchesApi.createBranch("Nairobi West", "key-create-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/branches");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-create-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // KEY EXACTNESS (falsifiable: add any key to the API layer and this fails).
  expect(Object.keys(body)).toEqual(["name"]);
  expect(body["name"]).toBe("Nairobi West");

  expect(record.name).toBe("Nairobi West");
  expect("internal_registry_note" in record).toBe(false);
});

test("PUT /branches/{id}: the body is exactly {version, name} pinning the acted-on record state (1.4); the renamed record parses verbatim", async () => {
  const record = await branchesApi.updateBranch(BRANCH_ID, 1, "Nairobi West HQ", "key-rename-1");
  const call = calls[0]!;
  expect(call.method).toBe("PUT");
  expect(new URL(call.url).pathname).toBe(`/branches/${BRANCH_ID}`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-rename-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual(["name", "version"]);
  expect(body["version"]).toBe(1);
  expect(body["name"]).toBe("Nairobi West HQ");
  expect(record.version).toBe(2);
});

test("PUT /branches/{id}/users/{user_id}: both ids travel as PATH parameters on the exact route; the body is exactly {version} — the USER row's pinned version (the access_control:edit route)", async () => {
  const result = await branchesApi.assignUserToBranch(BRANCH_ID, TARGET_USER_ID, 3, "key-au-1");
  const call = calls[0]!;
  expect(call.method).toBe("PUT");
  expect(new URL(call.url).pathname).toBe(`/branches/${BRANCH_ID}/users/${TARGET_USER_ID}`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-au-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 3 });
  expect(result.entity_id).toBe(TARGET_USER_ID);
  expect(result.branch_name).toBe("Nairobi West");
  expect(result.version).toBe(4);
});

test("PUT /branches/{id}/members/{member_id}: the members:edit route — body exactly {version}, the MEMBER row's pinned version", async () => {
  const result = await branchesApi.assignMemberToBranch(BRANCH_ID, MEMBER_ID, 6, "key-am-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/branches/${BRANCH_ID}/members/${MEMBER_ID}`);
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 6 });
  expect(result.entity_id).toBe(MEMBER_ID);
  expect(result.version).toBe(7);
});

test("POST /jobs/branch-backfill: the body is exactly {batch_size: 200} — the SERVER's own documented default, never an operator value; the run report parses verbatim (side-effect counts); EXTRA keys are STRIPPED", async () => {
  const run = await branchesApi.runBranchBackfill("key-backfill-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe("/jobs/branch-backfill");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-backfill-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body)).toEqual(["batch_size"]);
  expect(body["batch_size"]).toBe(branchesApi.SERVER_DEFAULT_BATCH_SIZE);
  expect(body["batch_size"]).toBe(200);

  expect(run.scanned).toBe(12);
  expect(run.branches_created).toBe(3);
  expect(run.users_linked).toBe(12);
  expect("internal_scan_trace" in run).toBe(false);
});

test("a create 409 (duplicate name after normalisation) surfaces as ONE ApiError — no transport-level retry", async () => {
  createStatus = 409;
  const thrown = await branchesApi.createBranch("HQ", "key-create-dup").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a create 422 (the server's shape verdict) surfaces as ONE typed ApiError with the field messages", async () => {
  createStatus = 422;
  const thrown = await branchesApi.createBranch("", "key-create-422").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(422);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a rename 409 (stale version / name collision) surfaces as ONE ApiError — nothing changed, nothing replayed", async () => {
  updateStatus = 409;
  const thrown = await branchesApi
    .updateBranch(BRANCH_ID, 1, "HQ", "key-rename-stale")
    .catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});

test("an assignment 409 (the USER row moved underneath the pinned version) surfaces as ONE ApiError", async () => {
  assignStatus = 409;
  const thrown = await branchesApi
    .assignUserToBranch(BRANCH_ID, TARGET_USER_ID, 2, "key-au-stale")
    .catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});

test("a garbage created_at is REJECTED at the boundary (the !63 F-R4 lesson) — never rendered 'Invalid Date'", async () => {
  listGarbageTimestamp = true;
  const thrown = await branchesApi.fetchBranchesPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("created_at");
});

test("accept/reject matrix (hand-computed oracles): timestamps, integers-never-strings, key exactness across all three response shapes — and NO money field exists on this contract to assert", () => {
  const branchWith = (field: string, value: unknown) =>
    branchSchemas.branchSchema.safeParse({ ...branchOut, [field]: value }).success;

  expect(branchWith("created_at", "2026-08-01T09:00:00.123456+00:00")).toBe(true);
  expect(branchWith("created_at", "yesterday-ish")).toBe(false);
  expect(branchWith("created_at", null)).toBe(false); // NOT nullable on this contract
  expect(branchWith("version", "1")).toBe(false);
  expect(branchWith("name", 42)).toBe(false);

  // Key exactness: EVERY BranchOut key is required (no nullable field
  // exists on this contract).
  for (const key of Object.keys(branchOut)) {
    const missing: Record<string, unknown> = { ...branchOut };
    delete missing[key];
    expect(branchSchemas.branchSchema.safeParse(missing).success).toBe(false);
  }

  const assignWith = (field: string, value: unknown) =>
    branchSchemas.branchAssignmentSchema.safeParse({ ...assignmentOut, [field]: value }).success;
  expect(assignWith("version", "4")).toBe(false);
  for (const key of Object.keys(assignmentOut)) {
    const missing: Record<string, unknown> = { ...assignmentOut };
    delete missing[key];
    expect(branchSchemas.branchAssignmentSchema.safeParse(missing).success).toBe(false);
  }

  const runWith = (field: string, value: unknown) =>
    branchSchemas.backfillRunSchema.safeParse({ ...backfillOut, [field]: value }).success;
  // A completed re-run legitimately reports all zeros (the lock-free
  // no-op is PROVEN by counts)…
  expect(
    branchSchemas.backfillRunSchema.safeParse({
      scanned: 0,
      branches_created: 0,
      users_linked: 0,
      batches: 0,
    }).success,
  ).toBe(true);
  // …but counts are integers, never numeric strings or fractions.
  expect(runWith("scanned", "12")).toBe(false);
  expect(runWith("users_linked", 1.5)).toBe(false);

  // The contract carries exactly these keys — no money field exists,
  // and none is asserted (fake validation is a rejected posture).
  expect(Object.keys(branchOut).sort()).toEqual(["created_at", "id", "name", "version"]);
});
