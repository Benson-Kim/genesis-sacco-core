/**
 * @jest-environment node
 *
 * Network-layer proofs for the member-exit API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the transactions/reports reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3);
 *   the status filter is a server-side query parameter.
 * - NO money parameter can even be SENT (P12: fees from tenant config,
 *   figures computed server-side under locks): every wire body is
 *   asserted key-by-key — create carries {member_id, reason}, vote
 *   {vote}, void {version}, settlement {version, channel} and nothing
 *   else.
 * - Response money arrives as decimal STRINGS byte-identically; money
 *   as numbers, unknown statuses and garbage timestamps are REJECTED
 *   at the Zod boundary (blocker (a) + the !63 F-R4 lesson); extra
 *   keys are STRIPPED.
 * - A 409 (settlement snapshot drift under the full lock set) surfaces
 *   as a typed ApiError from ONE request (no transport-level retry).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXIT_ID = "eeeeeeee-aaaa-bbbb-cccc-444444444444";
const REQUESTER_ID = "66666666-6666-6666-6666-666666666666";

const calls: FetchCall[] = [];
let settlementStatus = 201;
let listMoneyAsNumbers = false;
let listGarbageMoney = false;
let listUnknownStatus = false;
let listGarbageTimestamp = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const exitOut = {
  id: EXIT_ID,
  member_id: MEMBER_ID,
  status: "requested",
  reason: "Relocation",
  requested_by: REQUESTER_ID,
  shares_amount: "25000.10",
  deposits_amount: "150000.10",
  loan_balance: "40000.00",
  fees: "500.00",
  net_payable: "134500.20",
  decided_at: null,
  settled_at: null,
  settlement_txn_id: null,
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
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
  if (path === "/member-exits" && request.method === "GET") {
    if (listMoneyAsNumbers) {
      return json(200, { items: [{ ...exitOut, net_payable: 134500.2 }], next_cursor: null });
    }
    if (listGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the F-M2 shape
      // assertion is the only thing standing between it and fmtKes.
      return json(200, { items: [{ ...exitOut, fees: "007.10" }], next_cursor: null });
    }
    if (listUnknownStatus) {
      return json(200, { items: [{ ...exitOut, status: "escalated" }], next_cursor: null });
    }
    if (listGarbageTimestamp) {
      return json(200, {
        items: [{ ...exitOut, created_at: "yesterday-ish" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [exitOut], next_cursor: "cursor-page-2" });
  }
  if (path === "/member-exits" && request.method === "POST") {
    return json(201, { ...exitOut, internal_lock_note: "ROW-LOCK-SECRET" });
  }
  if (path === `/member-exits/${EXIT_ID}` && request.method === "GET") {
    return json(200, exitOut);
  }
  if (path === `/member-exits/${EXIT_ID}/votes` && request.method === "POST") {
    return json(201, { approvals: 1, rejections: 0, decision: null, status: "requested" });
  }
  if (path === `/member-exits/${EXIT_ID}/void` && request.method === "POST") {
    return json(200, { ...exitOut, status: "rejected", version: 2 });
  }
  if (path === `/member-exits/${EXIT_ID}/settlement` && request.method === "POST") {
    if (settlementStatus !== 201) {
      return json(settlementStatus, { category: "conflict", correlation_id: "corr-drift" });
    }
    return json(201, {
      exit: {
        ...exitOut,
        status: "settled",
        settled_at: "2026-08-02T10:00:00+00:00",
        settlement_txn_id: "dddddddd-1111-2222-3333-444444444444",
        version: 3,
      },
      txn_ref: "EX-0001",
    });
  }
  if (path === `/member-exits/${EXIT_ID}/statement` && request.method === "GET") {
    return json(200, {
      exit_id: EXIT_ID,
      member_id: MEMBER_ID,
      member_no: "M-0001",
      member_name: "Jane Wanjiku",
      member_status: "active",
      exit_status: "requested",
      reason: "Relocation",
      requested_by: REQUESTER_ID,
      shares_amount: "25000.10",
      deposits_amount: "150000.10",
      equity: "175000.20",
      loan_balance: "40000.00",
      fees: "500.00",
      net_payable: "134500.20",
      requested_at: "2026-08-01T09:00:00+00:00",
      decided_at: null,
      settled_at: null,
      settlement_txn_ref: null,
      internal_gl_breakdown: "GL-SECRET-02",
    });
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
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  settlementStatus = 201;
  listMoneyAsNumbers = false;
  listGarbageMoney = false;
  listUnknownStatus = false;
  listGarbageTimestamp = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /member-exits: keyset params ONLY (no offset/page); the opaque cursor echoes back VERBATIM; an empty status filter sends NOTHING", async () => {
  await exitsApi.fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, null);
  const page = await exitsApi.fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, "cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/member-exits");
  expect(first.searchParams.get("limit")).toBe(String(exitsApi.EXITS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  expect(first.searchParams.has("status")).toBe(false);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  // The boundary transform yields the client-side page shape verbatim —
  // trailing cents survive because the value never becomes a number.
  expect(page.items[0]?.net_payable).toBe("134500.20");
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("GET /member-exits: the status filter is a SERVER-side query parameter", async () => {
  await exitsApi.fetchExitsPage({ status: "approved" }, null);
  const url = new URL(calls[0]!.url);
  expect(url.searchParams.get("status")).toBe("approved");
  expect(url.searchParams.has("offset")).toBe(false);
  expect(url.searchParams.has("page")).toBe(false);
});

test("POST /member-exits: the body carries {member_id, reason} and NOTHING else — no money field can even be sent; EXTRA response keys are STRIPPED", async () => {
  const record = await exitsApi.createExitRequest(MEMBER_ID, "Relocation", "key-exit-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/member-exits");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-exit-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["member_id"]).toBe(MEMBER_ID);
  expect(body["reason"]).toBe("Relocation");
  // Nothing beyond the ExitRequestBody contract can even be sent —
  // falsifiable: add a money key to the API layer and this fails.
  expect(Object.keys(body).sort()).toEqual(["member_id", "reason"]);

  // The SERVER's snapshot comes back verbatim; the stubbed internal
  // field can never reach a screen (stripped at the boundary).
  expect(record.net_payable).toBe("134500.20");
  expect(record.shares_amount).toBe("25000.10");
  expect("internal_lock_note" in record).toBe(false);
});

test("POST votes: the body is exactly {vote}; the tally parses verbatim (server counts, never client-derived)", async () => {
  const tally = await exitsApi.voteOnExit(EXIT_ID, "approve", "key-vote-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/member-exits/${EXIT_ID}/votes`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-vote-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ vote: "approve" });
  expect(tally.approvals).toBe(1);
  expect(tally.rejections).toBe(0);
  expect(tally.decision).toBeNull();
  expect(tally.status).toBe("requested");
});

test("POST void: the body is exactly {version} pinning the acted-on record state (1.4)", async () => {
  const record = await exitsApi.voidExit(EXIT_ID, 1, "key-void-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/member-exits/${EXIT_ID}/void`);
  expect(call.headers.get("idempotency-key")).toBe("key-void-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 1 });
  expect(record.status).toBe("rejected");
  expect(record.version).toBe(2);
});

test("POST settlement: the body is exactly {version, channel} — the pinned snapshot version and the CASH rail; every result figure is the server's, verbatim", async () => {
  const posted = await exitsApi.postExitSettlement(EXIT_ID, 1, "mpesa", "key-settle-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/member-exits/${EXIT_ID}/settlement`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-settle-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 1, channel: "mpesa" });
  expect(posted.txn_ref).toBe("EX-0001");
  expect(posted.exit.status).toBe("settled");
  expect(posted.exit.net_payable).toBe("134500.20");
  expect(posted.exit.settlement_txn_id).toBe("dddddddd-1111-2222-3333-444444444444");
});

test("a drifted settlement (snapshot re-verification failed under the lock set) surfaces as ONE 409 ApiError — no transport-level retry, no replay", async () => {
  settlementStatus = 409;
  const thrown = await exitsApi
    .postExitSettlement(EXIT_ID, 1, "bank", "key-settle-stale")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a contract-violating response (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  listMoneyAsNumbers = true;
  const thrown = await exitsApi
    .fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("net_payable");
});

test("an unknown exit status is a contract violation and is REJECTED — it can never arm a lifecycle write", async () => {
  listUnknownStatus = true;
  const thrown = await exitsApi
    .fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("status");
});

test("a garbage timestamp is REJECTED at the boundary (the !63 F-R4 lesson) — 'Invalid Date' can never render on the financial record", async () => {
  listGarbageTimestamp = true;
  const thrown = await exitsApi
    .fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("created_at");
});

test("a garbage money STRING is REJECTED at the wire boundary (review F-M2) — a value that passes z.string() can still never reach fmtKes", async () => {
  listGarbageMoney = true;
  const thrown = await exitsApi
    .fetchExitsPage(exitsApi.EMPTY_EXIT_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("fees");
});

test("F-M2 accept/reject matrix: money fields assert the CANONICAL server decimal shape (numeric(18,2) via str(Decimal)); the sign survives ONLY on net_payable", () => {
  const exitWith = (field: string, value: string) =>
    exitSchemas.exitSchema.safeParse({ ...exitOut, [field]: value }).success;

  // Canonical shapes ACCEPTED — exactly the backend serialisation.
  expect(exitWith("shares_amount", "0.00")).toBe(true);
  expect(exitWith("shares_amount", "25000.10")).toBe(true);
  expect(exitWith("fees", "500.00")).toBe(true);
  expect(exitWith("net_payable", "134500.20")).toBe(true);
  // The DOCUMENTED negative branch: net_payable keeps its sign.
  expect(exitWith("net_payable", "-4000.00")).toBe(true);

  // Garbage shapes REJECTED on every money field — none of these is a
  // str(Decimal) of a numeric(18,2) value, and each would previously
  // have flowed into fmtKes on a financial document.
  const garbage = [
    "abc",
    "1e5",
    "007.10", // leading zeros beyond a lone 0
    "25000.1", // wrong scale (the server always emits two places)
    "25000.100",
    "25000", // missing scale
    "25,000.10", // grouped — grouping is fmtKes's job, never the wire's
    " 25000.10",
    "25000.10 ",
    "NaN",
    "Infinity",
    "0x10",
    "",
  ];
  for (const field of ["shares_amount", "deposits_amount", "loan_balance", "fees", "net_payable"]) {
    for (const value of garbage) {
      expect(exitWith(field, value)).toBe(false);
    }
  }

  // A '-' is a CONTRACT VIOLATION on every CHECK (>= 0) column — only
  // net_payable (no CHECK, migration 0010) legitimately carries it.
  for (const field of ["shares_amount", "deposits_amount", "loan_balance", "fees"]) {
    expect(exitWith(field, "-1.00")).toBe(false);
  }

  // The statement document boundary mirrors the same posture — equity
  // (the server sum of two non-negative components) rejects the sign.
  const statement = {
    exit_id: EXIT_ID,
    member_id: MEMBER_ID,
    member_no: "M-0001",
    member_name: "Jane Wanjiku",
    member_status: "active",
    exit_status: "requested",
    reason: null,
    requested_by: null,
    shares_amount: "25000.10",
    deposits_amount: "150000.10",
    equity: "175000.20",
    loan_balance: "40000.00",
    fees: "500.00",
    net_payable: "-4000.00",
    requested_at: "2026-08-01T09:00:00+00:00",
    decided_at: null,
    settled_at: null,
    settlement_txn_ref: null,
  };
  expect(exitSchemas.exitStatementSchema.safeParse(statement).success).toBe(true);
  expect(
    exitSchemas.exitStatementSchema.safeParse({ ...statement, equity: "-175000.20" }).success,
  ).toBe(false);
  expect(
    exitSchemas.exitStatementSchema.safeParse({ ...statement, equity: "1e5" }).success,
  ).toBe(false);
  expect(
    exitSchemas.exitStatementSchema.safeParse({ ...statement, net_payable: "abc" }).success,
  ).toBe(false);
});

test("GET statement: the canonical document parses VERBATIM (server equity line included); EXTRA keys are STRIPPED", async () => {
  const statement = await exitsApi.fetchExitStatement(EXIT_ID);
  expect(new URL(calls[0]!.url).pathname).toBe(`/member-exits/${EXIT_ID}/statement`);
  expect(statement.member_no).toBe("M-0001");
  // The equity line is the SERVER's — the client never re-derives it
  // from shares/deposits (blocker (a)); trailing cents survive.
  expect(statement.equity).toBe("175000.20");
  expect(statement.net_payable).toBe("134500.20");
  expect("internal_gl_breakdown" in statement).toBe(false);
  // Initiator attribution (issue #30 / !66 follow-up): the bare staff
  // UUID travels verbatim through the document boundary — least
  // disclosure: no name/email key exists for the INITIATOR anywhere
  // in the contract or the parse (member_name is the exiting MEMBER's
  // own document line, not the staff actor's).
  expect(statement.requested_by).toBe(REQUESTER_ID);
  expect("full_name" in statement).toBe(false);
  expect("email" in statement).toBe(false);
});

test("attribution accept/reject matrix (issue #30 / !66 follow-up): requested_by is a nullable STRING on BOTH boundaries — the NULL leg is the honest 'unattributed' wire value; a MISSING key is a contract violation", async () => {
  // The record read carries the initiator verbatim; no name/email key
  // exists anywhere in the parse (least disclosure, FM-D).
  const record = await exitsApi.fetchExit(EXIT_ID);
  expect(record.requested_by).toBe(REQUESTER_ID);
  expect("full_name" in record).toBe(false);
  expect("email" in record).toBe(false);

  const withField = (value: unknown) =>
    exitSchemas.exitSchema.safeParse({ ...exitOut, requested_by: value }).success;

  // The two legitimate wire values: the bare staff UUID and the honest
  // NULL (pre-0036 rows — attribution never invented, FM-B).
  expect(withField(REQUESTER_ID)).toBe(true);
  expect(withField(null)).toBe(true);

  // Shape drift REJECTED: attribution never arrives as a number, an
  // object (a smuggled name/email payload) or a boolean.
  expect(withField(42)).toBe(false);
  expect(withField({ id: REQUESTER_ID, full_name: "Jane" })).toBe(false);
  expect(withField(true)).toBe(false);

  // KEY EXACTNESS: dropping the key entirely is a contract violation —
  // the field is nullable, not optional (falsifiable: soften the schema
  // to .optional() and this leg fails).
  const missing: Record<string, unknown> = { ...exitOut };
  delete missing["requested_by"];
  expect(exitSchemas.exitSchema.safeParse(missing).success).toBe(false);

  // The statement DOCUMENT boundary mirrors the same posture.
  const statement = await exitsApi.fetchExitStatement(EXIT_ID);
  const statementWith = (value: unknown) =>
    exitSchemas.exitStatementSchema.safeParse({ ...statement, requested_by: value }).success;
  expect(statementWith(REQUESTER_ID)).toBe(true);
  expect(statementWith(null)).toBe(true);
  expect(statementWith(42)).toBe(false);
  expect(statementWith({ id: REQUESTER_ID, full_name: "Jane" })).toBe(false);
  const statementMissing: Record<string, unknown> = { ...statement };
  delete statementMissing["requested_by"];
  expect(exitSchemas.exitStatementSchema.safeParse(statementMissing).success).toBe(false);
});
