/**
 * @jest-environment node
 *
 * Network-layer proofs for the transactions API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the loans/applications reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3);
 *   every register filter is a server-side query parameter.
 * - Money travels as decimal STRINGS byte-identically in JSON bodies
 *   (blocker (a)) — never re-encoded as numbers; response money as
 *   numbers is REJECTED at the Zod boundary.
 * - The interest-run body carries the contract-required `batch_size`
 *   at its documented default ONLY (a server-bounded operational tuning
 *   value — T1/479fb5d5). No money parameter exists in the body: rate
 *   and period remain server-owned (v1.1 rule 1).
 * - A 409 (overdraw under the account row lock) surfaces as a typed
 *   ApiError from ONE request (no transport-level retry).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const TXN_ID = "eeeeeeee-1111-2222-3333-444444444444";
const TELLER_ID = "66666666-6666-6666-6666-666666666666";

const calls: FetchCall[] = [];
let withdrawalStatus = 201;
let listMoneyAsNumbers = false;
let listUnknownType = false;
let listGarbageMoney = false;
let listGarbageTimestamp = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const txnOut = {
  id: TXN_ID,
  txn_ref: "MP-1042",
  member_id: MEMBER_ID,
  type: "deposit",
  amount: "50000.10",
  channel: "mpesa",
  direction: "credit",
  occurred_at: "2026-08-01T09:00:00+00:00",
  is_reversal: false,
  created_by: TELLER_ID,
  external_ref: "SGH3KLM9QT",
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
  if (path === "/transactions" && request.method === "GET") {
    if (listMoneyAsNumbers) {
      return json(200, { items: [{ ...txnOut, amount: 50000.1 }], next_cursor: null });
    }
    if (listUnknownType) {
      return json(200, { items: [{ ...txnOut, type: "gl_sweep" }], next_cursor: null });
    }
    if (listGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the shared
      // moneySchema shape (issue #30 A2/S2) is the only thing standing
      // between it and fmtKes on the register.
      return json(200, { items: [{ ...txnOut, amount: "007.10" }], next_cursor: null });
    }
    if (listGarbageTimestamp) {
      return json(200, {
        items: [{ ...txnOut, occurred_at: "yesterday-ish" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [txnOut], next_cursor: "cursor-page-2" });
  }
  if (path === `/members/${MEMBER_ID}/deposits` && request.method === "POST") {
    return json(201, {
      txn_id: TXN_ID,
      txn_ref: "MP-1042",
      amount: "50000.10",
      balance_after: "173500.10",
      internal_gl_note: "GL-SECRET-01",
    });
  }
  if (path === `/members/${MEMBER_ID}/withdrawals` && request.method === "POST") {
    if (withdrawalStatus !== 201) {
      return json(withdrawalStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    return json(201, {
      txn_id: TXN_ID,
      txn_ref: "WD-0221",
      amount: "8000.00",
      balance_after: "165500.10",
    });
  }
  if (path === `/members/${MEMBER_ID}/share-topups` && request.method === "POST") {
    return json(201, {
      txn_id: TXN_ID,
      txn_ref: "SH-0107",
      amount: "2500.10",
      balance_after: "42500.10",
    });
  }
  if (path === "/jobs/deposit-interest" && request.method === "POST") {
    return json(200, {
      period_start: "2026-04-01",
      period_end: "2026-06-30",
      annual_rate_pct: "8.00",
      scanned: 120,
      posted: 118,
      skipped_existing: 2,
      rate_mismatches: 0,
      total_interest: "45678.10",
      batches: 2,
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
const txnApi = require("../api") as typeof import("../api");
const txnSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  withdrawalStatus = 201;
  listMoneyAsNumbers = false;
  listUnknownType = false;
  listGarbageMoney = false;
  listGarbageTimestamp = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /transactions: keyset params ONLY (no offset/page); the opaque cursor echoes back VERBATIM; empty filters send NOTHING", async () => {
  await txnApi.fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, null);
  const page = await txnApi.fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, "cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/transactions");
  expect(first.searchParams.get("limit")).toBe(String(txnApi.TXNS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  // Empty filters never travel as empty-string params.
  for (const param of ["member_id", "type", "channel", "direction", "ref", "date_from", "date_to"]) {
    expect(first.searchParams.has(param)).toBe(false);
  }

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  // The boundary transform yields the client-side page shape verbatim.
  expect(page.items[0]?.amount).toBe("50000.10");
  expect(page.nextCursor).toBe("cursor-page-2");
  // Posting-actor attribution (issue #30 / !66 follow-up): the bare
  // staff UUID travels verbatim — least disclosure: no name/email key
  // exists anywhere in the contract or the parse.
  expect(page.items[0]?.created_by).toBe(TELLER_ID);
  expect(page.items[0] !== undefined && "full_name" in page.items[0]).toBe(false);
  expect(page.items[0] !== undefined && "email" in page.items[0]).toBe(false);
});

test("attribution accept/reject matrix (issue #30 / !66 follow-up): created_by is a nullable STRING — the NULL leg is the honest system-posting wire value; a MISSING key is a contract violation", () => {
  const withField = (value: unknown) =>
    txnSchemas.transactionSchema.safeParse({ ...txnOut, created_by: value }).success;

  // The two legitimate wire values: the bare staff UUID and the honest
  // NULL (system/job postings — interest, dormancy — and pre-0036 rows;
  // attribution never invented).
  expect(withField(TELLER_ID)).toBe(true);
  expect(withField(null)).toBe(true);

  // Shape drift REJECTED: attribution never arrives as a number, an
  // object (a smuggled name/email payload) or a boolean.
  expect(withField(42)).toBe(false);
  expect(withField({ id: TELLER_ID, full_name: "Jane" })).toBe(false);
  expect(withField(true)).toBe(false);

  // KEY EXACTNESS: dropping the key entirely is a contract violation —
  // the field is nullable, not optional (falsifiable: soften the schema
  // to .optional() and this leg fails).
  const missing: Record<string, unknown> = { ...txnOut };
  delete missing["created_by"];
  expect(txnSchemas.transactionSchema.safeParse(missing).success).toBe(false);
});

test("GET /transactions: every register filter is a SERVER-side query parameter", async () => {
  await txnApi.fetchTransactionsPage(
    {
      member_id: MEMBER_ID,
      type: "withdrawal",
      channel: "bank",
      direction: "debit",
      ref: "WD-0221",
      search: "wanjiku",
      date_from: "2026-07-01",
      date_to: "2026-07-31",
    },
    null,
  );
  const url = new URL(calls[0]!.url);
  expect(url.searchParams.get("member_id")).toBe(MEMBER_ID);
  expect(url.searchParams.get("type")).toBe("withdrawal");
  expect(url.searchParams.get("channel")).toBe("bank");
  expect(url.searchParams.get("direction")).toBe("debit");
  expect(url.searchParams.get("ref")).toBe("WD-0221");
  expect(url.searchParams.get("search")).toBe("wanjiku");
  expect(url.searchParams.get("date_from")).toBe("2026-07-01");
  expect(url.searchParams.get("date_to")).toBe("2026-07-31");
  expect(url.searchParams.has("offset")).toBe(false);
  expect(url.searchParams.has("page")).toBe(false);
});

test("POST deposits: the amount travels as a decimal STRING byte-identically; idempotency as a header; EXTRA response keys are STRIPPED", async () => {
  const result = await txnApi.postDeposit(
    MEMBER_ID,
    { amount: "50000.10", channel: "mpesa", external_ref: "SGH3KLM9QT" },
    "key-dep-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe(`/members/${MEMBER_ID}/deposits`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-dep-1");
  // String on the wire — falsifiable: coerce to Number and the trailing
  // ".10" collapses / the JSON type flips. Assert the raw bytes too.
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["amount"]).toBe("50000.10");
  expect(typeof body["amount"]).toBe("string");
  expect(call.body).toContain('"amount":"50000.10"');
  expect(body["channel"]).toBe("mpesa");
  expect(body["external_ref"]).toBe("SGH3KLM9QT");
  // Nothing beyond the MoneyBody contract can even be sent.
  expect(Object.keys(body).sort()).toEqual(["amount", "channel", "external_ref"]);

  // The SERVER's figures come back verbatim; the stubbed internal
  // ledger field can never reach a screen (stripped at the boundary).
  expect(result.txn_ref).toBe("MP-1042");
  expect(result.balance_after).toBe("173500.10");
  expect("internal_gl_note" in result).toBe(false);
});

test("a stale withdrawal (overdraw under the row lock) surfaces as ONE 409 ApiError — no transport-level retry, no replay", async () => {
  withdrawalStatus = 409;
  const thrown = await txnApi
    .postWithdrawal(
      MEMBER_ID,
      { amount: "8000.00", channel: "bank", external_ref: "SLIP-90" },
      "key-wd-stale",
    )
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("POST share-topups + withdrawals ride their own routes with the same MoneyBody shape", async () => {
  const topup = await txnApi.postShareTopup(
    MEMBER_ID,
    { amount: "2500.10", channel: "bank", external_ref: "SLIP-91" },
    "key-sh-1",
  );
  expect(new URL(calls[0]!.url).pathname).toBe(`/members/${MEMBER_ID}/share-topups`);
  expect(calls[0]!.body).toContain('"amount":"2500.10"');
  expect(topup.txn_ref).toBe("SH-0107");
  expect(topup.balance_after).toBe("42500.10");

  const withdrawal = await txnApi.postWithdrawal(
    MEMBER_ID,
    { amount: "8000.00", channel: "bank", external_ref: "SLIP-92" },
    "key-wd-1",
  );
  expect(new URL(calls[1]!.url).pathname).toBe(`/members/${MEMBER_ID}/withdrawals`);
  expect(withdrawal.txn_ref).toBe("WD-0221");
  expect(withdrawal.balance_after).toBe("165500.10");
});

test("a contract-violating response (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  listMoneyAsNumbers = true;
  const thrown = await txnApi
    .fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("amount");
});

test("an unknown transaction type is a contract violation and is REJECTED, never silently rendered", async () => {
  listUnknownType = true;
  const thrown = await txnApi
    .fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("type");
});

test("a garbage money STRING is REJECTED at the wire boundary (issue #30 A2/S2) — a value that passes z.string() can still never reach fmtKes", async () => {
  listGarbageMoney = true;
  const thrown = await txnApi
    .fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("amount");
});

test("a garbage timestamp is REJECTED at the boundary (the !63 F-R4 lesson) — 'Invalid Date' can never render on the register", async () => {
  listGarbageTimestamp = true;
  const thrown = await txnApi
    .fetchTransactionsPage(txnApi.EMPTY_TXN_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("occurred_at");
});

test("issue #30 A2/S2 accept/reject matrix: money fields assert the CANONICAL server shape; the CHECK(>0)/CHECK(>=0) columns reject a sign", () => {
  const txnWith = (field: string, value: string) =>
    txnSchemas.transactionSchema.safeParse({ ...txnOut, [field]: value }).success;
  const accountOut = {
    txn_id: TXN_ID,
    txn_ref: "MP-1042",
    amount: "50000.10",
    balance_after: "173500.10",
  };
  const accountWith = (field: string, value: string) =>
    txnSchemas.accountTxnSchema.safeParse({ ...accountOut, [field]: value }).success;

  // Canonical shapes ACCEPTED — exactly the backend serialisation
  // (str(Decimal) of numeric(18,2) / to_cents; hand-computed oracles).
  expect(txnWith("amount", "0.01")).toBe(true);
  expect(txnWith("amount", "50000.10")).toBe(true);
  expect(accountWith("balance_after", "0.00")).toBe(true);
  expect(accountWith("amount", "2500.10")).toBe(true);

  // Garbage shapes REJECTED on every money field — none of these is a
  // str(Decimal) of a numeric(18,2) value, and each previously flowed
  // into fmtKes unchallenged (bare z.string()).
  const garbage = [
    "abc",
    "1e5",
    "007.10", // leading zeros beyond a lone 0
    "50000.1", // wrong scale — the server always emits two places
    "50000.100",
    "50000", // missing scale on a COLUMN value
    "50,000.10", // grouping is fmtKes's job, never the wire's
    " 50000.10",
    "NaN",
    "",
  ];
  for (const value of garbage) {
    expect(txnWith("amount", value)).toBe(false);
    expect(accountWith("amount", value)).toBe(false);
    expect(accountWith("balance_after", value)).toBe(false);
  }

  // A '-' is a CONTRACT VIOLATION everywhere here: transactions.amount
  // is CHECK (amount > 0) and deposit_accounts.balance is
  // CHECK (balance >= 0) (migration 0001) — no negative branch exists.
  expect(txnWith("amount", "-1.00")).toBe(false);
  expect(accountWith("balance_after", "-1.00")).toBe(false);

  // The interest-run boundary: total_interest is ZERO + to_cents sums
  // (two places even for an all-skipped run); the quarter bounds stay
  // DATE strings (deliberately NOT timestamp-asserted).
  const run = {
    period_start: "2026-04-01",
    period_end: "2026-06-30",
    annual_rate_pct: "8.00",
    scanned: 120,
    posted: 118,
    skipped_existing: 2,
    rate_mismatches: 0,
    total_interest: "45678.10",
    batches: 2,
  };
  expect(txnSchemas.interestRunSchema.safeParse(run).success).toBe(true);
  expect(
    txnSchemas.interestRunSchema.safeParse({ ...run, total_interest: "0.00" }).success,
  ).toBe(true);
  expect(
    txnSchemas.interestRunSchema.safeParse({ ...run, total_interest: "1e5" }).success,
  ).toBe(false);
  expect(
    txnSchemas.interestRunSchema.safeParse({ ...run, total_interest: "-1.00" }).success,
  ).toBe(false);
});

test("POST /jobs/deposit-interest: the body carries the batch size ONLY (no rate, period or as-of can be sent); the run result parses verbatim", async () => {
  const result = await txnApi.runDepositInterest("key-int-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe("/jobs/deposit-interest");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-int-1");
  // The sole caller-tunable field, at the contract's documented default
  // — never a money/rate/period parameter (v1.1 rule 1).
  expect(JSON.parse(call.body ?? "null")).toEqual({ batch_size: 200 });

  expect(result.period_start).toBe("2026-04-01");
  expect(result.annual_rate_pct).toBe("8.00");
  expect(result.total_interest).toBe("45678.10");
  expect(result.posted).toBe(118);
});
