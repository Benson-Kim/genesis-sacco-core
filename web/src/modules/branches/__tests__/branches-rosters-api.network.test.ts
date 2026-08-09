/**
 * @jest-environment node
 *
 * Network-layer proofs for the branch roster reads (#31 ledger (j).1)
 * through the REAL generated client + middleware (node environment;
 * fetch stubbed at the network boundary — the reference harness):
 * - GET only; the branch id travels as a PATH parameter; keyset
 *   params ONLY (cursor echoed VERBATIM, no offset/page/filter);
 *   bearer/tenant as HEADERS; nothing secret enters a URL.
 * - The Zod boundary REJECTS an unknown status (both vocabularies are
 *   the owning modules' pinned enums) and STRIPS widened rows.
 * - A 404 (unknown/cross-tenant branch — 404-before-facts server-side)
 *   surfaces as a typed ApiError from EXACTLY ONE request.
 */

// Module scope (the reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";
const MISSING_BRANCH_ID = "bbbbbbbb-9999-9999-9999-111111111111";

const calls: FetchCall[] = [];
let usersVariant: "ok" | "unknown-status" | "widened" = "ok";
let membersVariant: "ok" | "unknown-status" = "ok";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const USER_ROW = {
  id: "aaaaaaaa-1111-2222-3333-444444444444",
  full_name: "Ann Achieng",
  email: "ann@sacco.example",
  status: "active",
};

const MEMBER_ROW = {
  id: "cccccccc-1111-2222-3333-444444444444",
  member_no: "GP-7001",
  name: "Alpha Member",
  status: "dormant",
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
  if (path === `/branches/${BRANCH_ID}/users` && request.method === "GET") {
    if (usersVariant === "unknown-status") {
      return json(200, { items: [{ ...USER_ROW, status: "archived" }], next_cursor: null });
    }
    if (usersVariant === "widened") {
      // A future contract widening (a figure riding a roster row) is
      // STRIPPED at the boundary — least disclosure stays structural.
      return json(200, {
        items: [{ ...USER_ROW, deposits_total: "9999.00" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [USER_ROW], next_cursor: "roster-cursor-§2" });
  }
  if (path === `/branches/${BRANCH_ID}/members` && request.method === "GET") {
    if (membersVariant === "unknown-status") {
      return json(200, { items: [{ ...MEMBER_ROW, status: "frozen" }], next_cursor: null });
    }
    return json(200, { items: [MEMBER_ROW], next_cursor: null });
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

// The generated client captures globalThis.fetch at creation time.
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
const branchesApi = require("../api") as typeof import("../api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

beforeEach(() => {
  calls.length = 0;
  usersVariant = "ok";
  membersVariant = "ok";
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: "per-tab-refresh" });
});

afterEach(() => {
  session.clearSession();
});

test("GET /branches/{id}/users: PATH id, keyset params ONLY, cursor echoed VERBATIM; bearer/tenant as HEADERS; rows verbatim", async () => {
  const page1 = await branchesApi.fetchBranchUsersRosterPage(BRANCH_ID, null);
  await branchesApi.fetchBranchUsersRosterPage(BRANCH_ID, page1.nextCursor);

  expect(calls).toHaveLength(2);
  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe(`/branches/${BRANCH_ID}/users`);
  expect(calls[0]!.method).toBe("GET");
  expect(first.searchParams.get("limit")).toBe(String(branchesApi.ROSTER_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(first.href).not.toContain("per-tab-refresh");

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("roster-cursor-§2");

  expect(page1.items).toEqual([USER_ROW]);
});

test("GET /branches/{id}/members: PATH id, keyset params ONLY; the dormant status round-trips VERBATIM", async () => {
  const page = await branchesApi.fetchBranchMembersRosterPage(BRANCH_ID, null);

  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe(`/branches/${BRANCH_ID}/members`);
  expect(url.searchParams.has("offset")).toBe(false);
  expect(page.items).toEqual([MEMBER_ROW]);
});

test("Zod boundary: an unknown USER status is a contract violation — REJECTED (the owning module's pinned vocabulary)", async () => {
  usersVariant = "unknown-status";
  await expect(branchesApi.fetchBranchUsersRosterPage(BRANCH_ID, null)).rejects.toThrow();
});

test("Zod boundary: an unknown MEMBER status is a contract violation — REJECTED", async () => {
  membersVariant = "unknown-status";
  await expect(branchesApi.fetchBranchMembersRosterPage(BRANCH_ID, null)).rejects.toThrow();
});

test("a WIDENED roster row (a figure riding the contract) is STRIPPED at the boundary — identity facts only can reach a render", async () => {
  usersVariant = "widened";
  const page = await branchesApi.fetchBranchUsersRosterPage(BRANCH_ID, null);
  expect(page.items).toEqual([USER_ROW]);
  for (const row of page.items) {
    expect(Object.keys(row).sort()).toEqual(["email", "full_name", "id", "status"]);
  }
});

test("a 404 (unknown/cross-tenant branch — 404-before-facts server-side) surfaces as a typed ApiError from EXACTLY ONE request", async () => {
  await expect(
    branchesApi.fetchBranchUsersRosterPage(MISSING_BRANCH_ID, null),
  ).rejects.toMatchObject({ status: 404 });
  expect(calls).toHaveLength(1);
  const rejection = await branchesApi
    .fetchBranchMembersRosterPage(MISSING_BRANCH_ID, null)
    .catch((e: unknown) => e);
  expect(rejection).toBeInstanceOf(ApiError);
});
