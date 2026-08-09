/**
 * @jest-environment node
 *
 * Network-layer proofs for the member dividend-payout expand (#31
 * ledger (c)) through the REAL generated client + middleware (node
 * environment; fetch stubbed at the network boundary — the reference
 * harness):
 * - dividend_payout is NULLABLE, NOT OPTIONAL: a member row MISSING
 *   the key is a contract violation and is REJECTED at the Zod
 *   boundary — the falsifiable proof that the expand landed
 *   everywhere: on the flat list, the detail read AND the opted-in
 *   aggregates rows.
 * - A set preference round-trips VERBATIM as the server's code-owned
 *   vocabulary token (never re-labelled client-side); null rides as
 *   null — the honest "not set" state. The batch-3 aggregates object
 *   and the batch-7 branch_id ride unchanged next to it (TEN-key flat
 *   contract).
 */

// Module scope (the reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";

const calls: FetchCall[] = [];
let omitPayoutOnList = false;
let omitPayoutOnDetail = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

// Hand-written wire fixture: the TEN-key flat contract (batch-3 eight
// + branch_id + dividend_payout). "mpesa" is one of the server's own
// code-owned vocabulary tokens (domain/members.py DividendPayout) —
// asserted VERBATIM below, never mapped.
const memberOut = {
  id: MEMBER_ID,
  member_no: "GP-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active",
  version: 2,
  branch_id: null,
  dividend_payout: "mpesa",
};

const aggregates = {
  deposits_total: "1500.50",
  shares_total: "750.25",
  loans_outstanding: "2500.10",
  guarantees_pledged: "1000.40",
};

function withoutPayout(row: Record<string, unknown>): Record<string, unknown> {
  const rest = { ...row };
  delete rest["dividend_payout"];
  return rest;
}

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
  const url = new URL(request.url);
  if (request.headers.get("authorization") === null) {
    return json(401, { category: "unauthenticated", correlation_id: "corr-a" });
  }
  if (url.pathname === "/members" && request.method === "GET") {
    const row = omitPayoutOnList ? withoutPayout(memberOut) : memberOut;
    if (url.searchParams.get("include") === "aggregates") {
      return json(200, { items: [{ ...row, aggregates }], next_cursor: null });
    }
    return json(200, { items: [row], next_cursor: null });
  }
  if (url.pathname === `/members/${MEMBER_ID}` && request.method === "GET") {
    const row = omitPayoutOnDetail ? withoutPayout(memberOut) : memberOut;
    return json(200, { ...row, aggregates });
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
/* eslint-enable @typescript-eslint/no-require-imports */

beforeEach(() => {
  calls.length = 0;
  omitPayoutOnList = false;
  omitPayoutOnDetail = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: "per-tab-refresh" });
});

afterEach(() => {
  session.clearSession();
});

test("dividend_payout round-trips VERBATIM on the flat list, the aggregates rows AND the detail read — aggregates and branch_id ride unchanged", async () => {
  const flat = await membersApi.fetchMembersPage({ status: "", type: "" }, null);
  expect(flat.items[0]?.dividend_payout).toBe("mpesa");
  expect(flat.items[0]?.branch_id).toBeNull();

  const detailRows = await membersApi.fetchMembersPageWithAggregates(
    { status: "", type: "" },
    null,
  );
  expect(detailRows.items[0]?.dividend_payout).toBe("mpesa");
  expect(detailRows.items[0]?.aggregates).toEqual(aggregates);

  const detail = await membersApi.fetchMemberDetail(MEMBER_ID);
  expect(detail.dividend_payout).toBe("mpesa");
  expect(detail.aggregates).toEqual(aggregates);
});

test("NULLABLE, NOT OPTIONAL: a list row MISSING the dividend_payout key is a contract violation — REJECTED at the boundary", async () => {
  omitPayoutOnList = true;
  await expect(membersApi.fetchMembersPage({ status: "", type: "" }, null)).rejects.toThrow();
  await expect(
    membersApi.fetchMembersPageWithAggregates({ status: "", type: "" }, null),
  ).rejects.toThrow();
});

test("NULLABLE, NOT OPTIONAL: a detail read MISSING the dividend_payout key is REJECTED at the boundary", async () => {
  omitPayoutOnDetail = true;
  await expect(membersApi.fetchMemberDetail(MEMBER_ID)).rejects.toThrow();
});
