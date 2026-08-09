/**
 * @jest-environment node
 *
 * Network-layer proofs for the accounting-periods API layer through
 * the REAL generated client + middleware (node environment: real
 * Request/Response/Headers; fetch stubbed at the network boundary),
 * mirroring the exits/dividends reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3)
 *   and NO filter parameter exists at all (the P12.5 list contract
 *   declares none).
 * - The close body is asserted KEY-BY-KEY: exactly {year, month} —
 *   the only caller-chosen values on this contract; no date, bound,
 *   version or any other field can even be sent (extra="forbid"
 *   server-side; issue #12: never caller-backdatable). The write
 *   query string is EMPTY.
 * - Boundary accept/reject matrix: DATE-column bounds ("YYYY-MM-DD"
 *   exactly), the honest NULL vs a garbage close stamp, the
 *   migration-0012 status vocabulary (unknowns REJECTED), integers
 *   never numeric strings, nullable-not-optional key exactness.
 *   NO money field exists on this contract — the matrix asserts the
 *   shapes it ACTUALLY carries (asserting a money shape here would be
 *   fake validation).
 * - A 409 (month not elapsed / already closed) and a 422 (a smuggled
 *   field refused by extra="forbid") each surface as a typed ApiError
 *   from ONE request (no transport-level retry).
 * - DRIFT TRIPWIRE (the W57-5 pattern): the contract types
 *   PeriodOut.status as a plain string, so the mirrored vocabulary is
 *   pinned to the migration SOURCE OF TRUTH (0012's CHECK constraint)
 *   — a backend vocabulary change fails this suite.
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc) — the
// imports below already make this file a module.
import { readFileSync } from "node:fs";
import { join } from "node:path";

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";

const calls: FetchCall[] = [];
let closeStatus = 201;
let listUnknownStatus = false;
let listGarbageTimestamp = false;
let listGarbageDate = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const periodOut = {
  id: "pppppppp-aaaa-bbbb-cccc-111111111111",
  period_start: "2026-06-01",
  period_end: "2026-06-30",
  status: "closed",
  closed_at: "2026-07-01T08:00:00+00:00",
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
  if (path === "/accounting-periods" && request.method === "GET") {
    if (listUnknownStatus) {
      return json(200, { items: [{ ...periodOut, status: "frozen" }], next_cursor: null });
    }
    if (listGarbageTimestamp) {
      return json(200, { items: [{ ...periodOut, closed_at: "yesterday-ish" }], next_cursor: null });
    }
    if (listGarbageDate) {
      return json(200, { items: [{ ...periodOut, period_end: "30/06/2026" }], next_cursor: null });
    }
    return json(200, { items: [periodOut], next_cursor: "2026-06-01" });
  }
  if (path === "/accounting-periods/close" && request.method === "POST") {
    if (closeStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-closed" });
    }
    if (closeStatus === 422) {
      return json(422, {
        category: "validation",
        correlation_id: "corr-v",
        fields: { month: "must be between 1 and 12" },
      });
    }
    return json(201, { ...periodOut, internal_close_trace: "TRACE-SECRET" });
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
const periodsApi = require("../api") as typeof import("../api");
const periodSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  closeStatus = 201;
  listUnknownStatus = false;
  listGarbageTimestamp = false;
  listGarbageDate = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /accounting-periods: keyset params ONLY (no offset/page) and NO filter parameter at all; the opaque cursor echoes back VERBATIM; secrets ride as HEADERS", async () => {
  await periodsApi.fetchPeriodsPage(null);
  const page = await periodsApi.fetchPeriodsPage("2026-06-01");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/accounting-periods");
  expect(first.searchParams.get("limit")).toBe(String(periodsApi.PERIODS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  // The P12.5 list contract declares NO filters — none are sent.
  expect([...first.searchParams.keys()].sort()).toEqual(["limit"]);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("2026-06-01");
  expect(second.searchParams.has("offset")).toBe(false);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  expect(page.items[0]?.period_end).toBe("2026-06-30");
  expect(page.items[0]?.status).toBe("closed");
  expect(page.nextCursor).toBe("2026-06-01");
});

test("POST /accounting-periods/close: the body is exactly {year, month} — no date, bound, version or any other field can even be sent; the Idempotency-Key rides as a HEADER; the write query string is EMPTY; EXTRA response keys are STRIPPED", async () => {
  const record = await periodsApi.closePeriod(2026, 6, "key-close-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/accounting-periods/close");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-close-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // KEY EXACTNESS (falsifiable: add a date/bound/version key to the
  // API layer and this fails).
  expect(Object.keys(body).sort()).toEqual(["month", "year"]);
  expect(body["year"]).toBe(2026);
  expect(body["month"]).toBe(6);

  // The SERVER's row comes back verbatim; the stubbed internal field
  // can never reach a screen (stripped at the boundary).
  expect(record.period_start).toBe("2026-06-01");
  expect(record.closed_at).toBe("2026-07-01T08:00:00+00:00");
  expect("internal_close_trace" in record).toBe(false);
});

test("a close 409 (month not elapsed / already closed) surfaces as ONE ApiError — no transport-level retry, no replay", async () => {
  closeStatus = 409;
  const thrown = await periodsApi.closePeriod(2026, 7, "key-close-conflict").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a close 422 (the extra=\"forbid\" refusal shape) surfaces as ONE typed ApiError with the server's field verdicts", async () => {
  closeStatus = 422;
  const thrown = await periodsApi.closePeriod(2026, 13, "key-close-422").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(422);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("an unknown period status is a contract violation and is REJECTED at the boundary — it can never masquerade as period context", async () => {
  listUnknownStatus = true;
  const thrown = await periodsApi.fetchPeriodsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("status");
});

test("a garbage close timestamp is REJECTED at the boundary (the !63 F-R4 lesson)", async () => {
  listGarbageTimestamp = true;
  const thrown = await periodsApi.fetchPeriodsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("closed_at");
});

test("a garbage period bound is REJECTED at the boundary — the bounds are date.isoformat() calendar dates, nothing else", async () => {
  listGarbageDate = true;
  const thrown = await periodsApi.fetchPeriodsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("period_end");
});

test("accept/reject matrix (hand-computed oracles): DATE bounds, honest-NULL close stamp, 0012 status vocabulary, integers never strings — and NO money field exists on this contract to assert", () => {
  const rowWith = (field: string, value: unknown) =>
    periodSchemas.periodSchema.safeParse({ ...periodOut, [field]: value }).success;

  // Canonical shapes ACCEPTED — exactly the backend serialisation.
  expect(rowWith("period_start", "2026-06-01")).toBe(true);
  expect(rowWith("status", "open")).toBe(true);
  expect(rowWith("status", "closed")).toBe(true);
  expect(rowWith("closed_at", null)).toBe(true);
  expect(rowWith("closed_at", "2026-07-01T08:00:00.123456+00:00")).toBe(true);

  // DATE bounds: date.isoformat() ONLY — never datetimes, never
  // locale formats, never empty.
  for (const field of ["period_start", "period_end"]) {
    expect(rowWith(field, "2026-06-30T00:00:00")).toBe(false);
    expect(rowWith(field, "30/06/2026")).toBe(false);
    expect(rowWith(field, "")).toBe(false);
    expect(rowWith(field, null)).toBe(false);
  }

  // Status: the migration-0012 vocabulary exactly; unknowns REJECTED.
  expect(rowWith("status", "frozen")).toBe(false);
  expect(rowWith("status", "OPEN")).toBe(false);
  expect(rowWith("status", "")).toBe(false);

  // closed_at: nullable, but garbage is never rendered "Invalid Date".
  expect(rowWith("closed_at", "yesterday-ish")).toBe(false);
  expect(rowWith("closed_at", 1234567890)).toBe(false);

  // version: an integer, never a numeric string.
  expect(rowWith("version", "1")).toBe(false);

  // The contract carries exactly these keys — no money field exists,
  // and none is asserted (fake validation is a rejected posture).
  expect(Object.keys(periodOut).sort()).toEqual([
    "closed_at",
    "id",
    "period_end",
    "period_start",
    "status",
    "version",
  ]);
});

test("key exactness: dropping ANY contract key is a violation — closed_at is nullable, NOT optional (falsifiable: soften the schema to .optional() and this fails)", () => {
  const parse = (record: Record<string, unknown>) =>
    periodSchemas.periodSchema.safeParse(record).success;
  for (const key of Object.keys(periodOut)) {
    const missing: Record<string, unknown> = { ...periodOut };
    delete missing[key];
    expect(parse(missing)).toBe(false);
  }
});

test("DRIFT TRIPWIRE (W57-5 pattern): PERIOD_STATUSES is pinned to migration 0012's CHECK constraint — the vocabulary SOURCE OF TRUTH; a backend change fails here, never silently diverges", () => {
  const migrationPath = join(
    __dirname,
    "..", "..", "..", "..", "..",
    "backend", "migrations", "versions", "0012_accounting_periods.py",
  );
  const source = readFileSync(migrationPath, "utf8");
  const check = /CHECK \(status IN \(([^)]*)\)\)/.exec(source);
  if (check === null || check[1] === undefined) {
    throw new Error("status CHECK not found in 0012_accounting_periods.py — parser anchor drifted");
  }
  const vocabulary = [...check[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
  expect(vocabulary.length).toBeGreaterThanOrEqual(2);
  expect([...periodSchemas.PERIOD_STATUSES]).toEqual(vocabulary);
});
