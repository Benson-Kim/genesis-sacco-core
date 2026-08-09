/**
 * @jest-environment node
 *
 * Network-layer proofs through the REAL generated client + middleware
 * (node environment: real Request/Response/Headers; fetch stubbed at the
 * network boundary). Salvaged from duo/feature/p13-5-frontend-followthrough
 * @ 198a238 (originally ported from !25 web/tests/api.test.mjs) and adapted
 * to this scaffold's env-held tenant + per-tab refresh custody (P14):
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; the refresh token
 *   travels ONLY in POST bodies; nothing secret ever enters a URL.
 * - Keyset cursors pass through opaquely; no offset/page params exist.
 * - Silent refresh rotation is single-flight; a failed refresh tears the
 *   session down (401 => re-login).
 */

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const ROLE_ID = "66666666-6666-6666-6666-666666666666";

const calls: FetchCall[] = [];
let refreshStatus = 200;
let refreshCount = 0;
let lastIssuedAccess: string | null = null;
let userGarbageLastActive = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const userOut = {
  id: USER_ID,
  full_name: "Jane",
  email: "jane@sacco.co.ke",
  phone: null,
  branch: null,
  role_id: ROLE_ID,
  role_name: "Teller",
  status: "active",
  last_active_at: null,
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
  if (path === "/auth/refresh") {
    refreshCount += 1;
    if (refreshStatus !== 200) {
      return json(refreshStatus, { category: "unauthenticated", correlation_id: "corr-r" });
    }
    lastIssuedAccess = jwt(USER_ID, 900);
    return json(200, {
      access_token: lastIssuedAccess,
      refresh_token: `rotated-refresh-${refreshCount}`,
      expires_in: 900,
    });
  }
  if (request.headers.get("authorization") === null) {
    return json(401, { category: "unauthenticated", correlation_id: "corr-a" });
  }
  if (path === "/users" && request.method === "GET") {
    return json(200, { items: [userOut], next_cursor: null });
  }
  if (path === `/users/${USER_ID}` && request.method === "GET") {
    if (userGarbageLastActive) {
      return json(200, { ...userOut, last_active_at: "yesterday-ish" });
    }
    return json(200, userOut);
  }
  if (path === "/users" && request.method === "POST") {
    return json(201, userOut);
  }
  if (path === "/audit-log" && request.method === "GET") {
    return json(200, { items: [], next_cursor: null });
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

// The generated client captures globalThis.fetch at creation time, so the
// stub must be installed BEFORE the modules under test are imported.
globalThis.fetch = fetchStub as typeof globalThis.fetch;

// Tenant scope is build-time configuration on this scaffold (src/lib/env
// reads NEXT_PUBLIC_TENANT_ID at module load), so it must be set BEFORE
// the client modules are required — adapted from the salvage source's
// session-held tenant model.
process.env.NEXT_PUBLIC_TENANT_ID = TENANT;

// This scaffold's P14 custody model keeps the refresh token in per-tab
// sessionStorage (web/README.md security posture). The node test
// environment has no window, so back the SINGLE custody site with an
// in-memory Map — the real storage read/write/rotation path still runs.
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
const usersApi = require("../api") as typeof import("../api");
const userSchemas = require("../schemas") as typeof import("../schemas");
const auditApi = require("@/modules/audit/api") as typeof import("@/modules/audit/api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const ACCESS_MARKER = "eyJhbGciOiJIUzI1NiJ9"; // header segment of every fake JWT
const REFRESH_SECRET = "refresh-secret-token-1";

beforeEach(() => {
  calls.length = 0;
  refreshStatus = 200;
  refreshCount = 0;
  lastIssuedAccess = null;
  userGarbageLastActive = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_SECRET });
});

afterEach(() => {
  session.clearSession();
});

test("bearer + tenant + idempotency travel as headers; nothing secret in any URL", async () => {
  await usersApi.createUser(
    { full_name: "Jane", email: "jane@sacco.co.ke", role_id: ROLE_ID, phone: null, branch: null },
    "key-123",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.headers.get("idempotency-key")).toBe("key-123");
  // Tokens never appear in URLs (gate 1.6) — falsifiable: move auth to a
  // query parameter and this fails.
  expect(call.url).not.toContain(ACCESS_MARKER);
  expect(call.url).not.toContain(REFRESH_SECRET);
});

test("keyset cursors pass through opaquely; no offset/page parameters exist", async () => {
  const cursor = "b3BhcXVlIGN1cnNvcg==:tail";
  await usersApi.fetchUsersPage({ status: "active", roleId: ROLE_ID }, cursor);
  await auditApi.fetchAuditPage(
    { entity: "users", actorId: "", action: "", dateFrom: "", dateTo: "" },
    cursor,
  );
  expect(calls).toHaveLength(2);
  for (const call of calls) {
    const url = new URL(call.url);
    expect(url.searchParams.get("cursor")).toBe(cursor);
    expect(url.searchParams.has("offset")).toBe(false);
    expect(url.searchParams.has("page")).toBe(false);
    expect(url.searchParams.get("limit")).toBe("20");
  }
});

test("expired access token => silent single-flight refresh rotation; refresh token ONLY in the POST body", async () => {
  session.setSession({ accessToken: jwt(USER_ID, 5), refreshToken: REFRESH_SECRET });
  // Two parallel requests race the same expiry — exactly ONE rotation.
  await Promise.all([usersApi.fetchUser(USER_ID), usersApi.fetchUser(USER_ID)]);

  const refreshCalls = calls.filter((call) => call.url.endsWith("/auth/refresh"));
  expect(refreshCalls).toHaveLength(1);
  const refresh = refreshCalls[0]!;
  expect(refresh.method).toBe("POST");
  expect(JSON.parse(refresh.body ?? "{}")).toEqual({ refresh_token: REFRESH_SECRET });
  expect(refresh.url).not.toContain(REFRESH_SECRET);

  // The data requests carry the ROTATED access token.
  const dataCalls = calls.filter((call) => !call.url.endsWith("/auth/refresh"));
  expect(dataCalls.length).toBeGreaterThan(0);
  for (const call of dataCalls) {
    expect(call.headers.get("authorization")).toBe(`Bearer ${lastIssuedAccess}`);
  }
  // The rotated refresh token is now the session's (rotation observed).
  expect(session.getRefreshToken()).toBe("rotated-refresh-1");
});

test("failed refresh tears the session down and surfaces 401 (re-login flow)", async () => {
  refreshStatus = 401;
  session.setSession({ accessToken: jwt(USER_ID, 5), refreshToken: REFRESH_SECRET });

  await expect(usersApi.fetchUser(USER_ID)).rejects.toMatchObject({ status: 401 });
  expect(session.hasSession()).toBe(false);

  const thrown = await usersApi.fetchUser(USER_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
});

test("a garbage last_active_at is REJECTED at the wire boundary (issue #30 A2/S2) — the record can never reach fmtDateTime/relTime", async () => {
  userGarbageLastActive = true;
  const thrown = await usersApi.fetchUser(USER_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("last_active_at");
});

test("issue #30 A2/S2 accept/reject matrix: last_active_at asserts the datetime.isoformat() shape (api/users.py — null only when the column is unset)", () => {
  const withLastActive = (value: string | null) =>
    userSchemas.userSchema.safeParse({ ...userOut, last_active_at: value }).success;

  // Legitimate serialisations ACCEPTED (hand-computed from
  // datetime.isoformat(): naive, fractional-seconds, offset, Zulu).
  expect(withLastActive(null)).toBe(true);
  expect(withLastActive("2026-07-30T12:00:00")).toBe(true);
  expect(withLastActive("2026-07-30T12:00:00.123456")).toBe(true);
  expect(withLastActive("2026-07-30T12:00:00+00:00")).toBe(true);
  expect(withLastActive("2026-07-30T12:00:00Z")).toBe(true);

  // Garbage REJECTED — each previously flowed into fmtDateTime/relTime
  // unchallenged (bare z.string()) and rendered "Invalid Date".
  for (const value of [
    "yesterday-ish",
    "2026-07-30",
    "2026-07-30 12:00:00",
    "30/07/2026T12:00:00",
    "2026-07-30T12:00",
    "",
  ]) {
    expect(withLastActive(value)).toBe(false);
  }
});
