/**
 * @jest-environment node
 *
 * Network-layer proofs for the loan-book API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the applications/settings reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3).
 * - Money travels as decimal STRINGS byte-identically in JSON bodies
 *   (blocker (a)) — never re-encoded as numbers.
 * - The disburse body carries the CHANNEL only — no caller-sent money
 *   fields exist in this client (v1.1 rule 1).
 * - A stale disbursement surfaces as a typed 409 ApiError from ONE
 *   request (no transport-level retry); a contract-violating response
 *   (numeric money) is REJECTED at the Zod boundary, never returned.
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
const LOAN_ID = "cccccccc-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let disburseStatus = 201;
let loanMoneyAsNumbers = false;
let loanGarbageMoney = false;
let loanGarbageTimestamp = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const loanOut = {
  id: LOAN_ID,
  application_id: APP_ID,
  member_id: MEMBER_ID,
  product_id: PRODUCT_ID,
  principal: "2000000.00",
  balance: "1234567.10",
  rate_pct: "12.50",
  term_months: 36,
  status: "active",
  classification: "watch",
  days_past_due: 12,
  provision_pct: "5.00",
  penalty_due: "1500.10",
  disbursed_at: "2026-01-15T09:00:00Z",
  closed_at: null,
  version: 7,
};

const scheduleRowOut = {
  installment_no: 1,
  due_date: "2026-02-15",
  principal_due: "50000.10",
  interest_due: "20833.33",
  total_due: "70833.43",
  paid_amount: "0.00",
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
  if (path === "/loans" && request.method === "GET") {
    return json(200, { items: [loanOut], next_cursor: "cursor-page-2" });
  }
  if (path === `/loans/${LOAN_ID}` && request.method === "GET") {
    if (loanMoneyAsNumbers) {
      return json(200, { ...loanOut, balance: 1234567.1 });
    }
    if (loanGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the shared
      // moneySchema shape (issue #30 A2/S2) is the only thing standing
      // between it and fmtKes on the loan drawer.
      return json(200, { ...loanOut, penalty_due: "007.10" });
    }
    if (loanGarbageTimestamp) {
      return json(200, { ...loanOut, disbursed_at: "yesterday-ish" });
    }
    return json(200, loanOut);
  }
  if (path === `/loans/${LOAN_ID}/schedule` && request.method === "GET") {
    return json(200, [scheduleRowOut]);
  }
  if (path === `/loans/${LOAN_ID}/settlement-quote` && request.method === "GET") {
    return json(200, {
      as_of: "2026-08-03",
      principal_balance: "1234567.10",
      interest_due: "10000.10",
      penalties_due: "1500.10",
      total: "1246067.30",
    });
  }
  if (path === "/portfolio/summary" && request.method === "GET") {
    return json(200, {
      active_loans: 12,
      outstanding_balance: "9876543.10",
      npl_balance: "1111111.10",
      npl_ratio_pct: "12.50",
      par30_balance: "2000000.00",
      par30_ratio_pct: "20.25",
      provisions: "456789.10",
      penalties_due: "9999.10",
      by_classification: [],
    });
  }
  if (path === `/applications/${APP_ID}/disburse` && request.method === "POST") {
    if (disburseStatus !== 201) {
      return json(disburseStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    return json(201, {
      loan_id: LOAN_ID,
      txn_id: "eeeeeeee-1111-2222-3333-444444444444",
      txn_ref: "TXN-0001",
      schedule: [scheduleRowOut],
      internal_gl_account: "GL-SECRET-01",
    });
  }
  if (path === `/loans/${LOAN_ID}/repayments` && request.method === "POST") {
    return json(201, {
      txn_id: "ffffffff-1111-2222-3333-444444444444",
      txn_ref: "TXN-0007",
      penalties: "1500.10",
      interest: "3499.90",
      principal: "0.10",
      balance_after: "1229567.00",
      penalty_due_after: "0.00",
      status: "active",
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
const loansApi = require("../api") as typeof import("../api");
const loanSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  disburseStatus = 201;
  loanMoneyAsNumbers = false;
  loanGarbageMoney = false;
  loanGarbageTimestamp = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /loans: keyset params ONLY (status/classification/cursor/limit — no offset/page); the opaque cursor echoes back VERBATIM", async () => {
  await loansApi.fetchLoansPage({ status: "active", classification: "watch" }, null);
  const page = await loansApi.fetchLoansPage(
    { status: "active", classification: "watch" },
    "cursor-page-2",
  );
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/loans");
  expect(first.searchParams.get("status")).toBe("active");
  expect(first.searchParams.get("classification")).toBe("watch");
  expect(first.searchParams.get("limit")).toBe(String(loansApi.LOANS_PAGE_SIZE));
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
  expect(page.items[0]?.balance).toBe("1234567.10");
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("POST /applications/{id}/disburse: the body carries the CHANNEL only; id rides the PATH; idempotency as a header; extra response keys are STRIPPED", async () => {
  const result = await loansApi.disburseApplication(APP_ID, "mpesa", "key-disburse-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe(`/applications/${APP_ID}/disburse`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-disburse-1");
  // No caller-sent money field exists in this client (v1.1 rule 1).
  expect(JSON.parse(call.body ?? "{}")).toEqual({ channel: "mpesa" });

  // The Zod boundary strips anything beyond the contract — the stubbed
  // internal ledger field can never reach a screen.
  expect(result.loan_id).toBe(LOAN_ID);
  expect(result.txn_ref).toBe("TXN-0001");
  expect("internal_gl_account" in result).toBe(false);
  expect(result.schedule[0]?.total_due).toBe("70833.43");
});

test("a stale disbursement surfaces as ONE 409 ApiError — no transport-level retry, no replay", async () => {
  disburseStatus = 409;
  const thrown = await loansApi
    .disburseApplication(APP_ID, "bank", "key-disburse-stale")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("POST /loans/{id}/repayments: the amount travels as a decimal STRING byte-identically; idempotency as a header", async () => {
  const result = await loansApi.recordRepayment(
    LOAN_ID,
    { amount: "5000.10", channel: "mpesa" },
    "key-repay-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/loans/${LOAN_ID}/repayments`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-repay-1");
  // String on the wire — falsifiable: coerce to Number and the trailing
  // ".10" collapses / the JSON type flips. Assert the raw bytes too.
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["amount"]).toBe("5000.10");
  expect(typeof body["amount"]).toBe("string");
  expect(call.body).toContain('"amount":"5000.10"');

  // The parsed allocation is the server's, verbatim.
  expect(result.penalties).toBe("1500.10");
  expect(result.balance_after).toBe("1229567.00");
  expect(result.status).toBe("active");
});

test("GET /loans/{id}/settlement-quote: no caller-sent as-of; the quote parses through the Zod boundary verbatim", async () => {
  const quote = await loansApi.fetchSettlementQuote(LOAN_ID);
  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe(`/loans/${LOAN_ID}/settlement-quote`);
  // The server resolves the as-of date; this client sends none.
  expect(url.search).toBe("");
  expect(quote.total).toBe("1246067.30");
  expect(quote.as_of).toBe("2026-08-03");
});

test("a contract-violating response (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  loanMoneyAsNumbers = true;
  const thrown = await loansApi.fetchLoan(LOAN_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("balance");
});

test("a garbage money STRING is REJECTED at the wire boundary (issue #30 A2/S2) — a value that passes z.string() can still never reach fmtKes", async () => {
  loanGarbageMoney = true;
  const thrown = await loansApi.fetchLoan(LOAN_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("penalty_due");
});

test("a garbage timestamp is REJECTED at the boundary (the !63 F-R4 lesson) — 'Invalid Date' can never render on the loan drawer", async () => {
  loanGarbageTimestamp = true;
  const thrown = await loansApi.fetchLoan(LOAN_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("disbursed_at");
});

test("issue #30 A2/S2 accept/reject matrix: loan/schedule/quote/repayment money asserts the CANONICAL shape; portfolio aggregates admit the bare '0'; no CHECK(>=0) column accepts a sign", () => {
  const loanWith = (field: string, value: string) =>
    loanSchemas.loanSchema.safeParse({ ...loanOut, [field]: value }).success;
  const rowWith = (field: string, value: string) =>
    loanSchemas.scheduleRowSchema.safeParse({ ...scheduleRowOut, [field]: value }).success;

  // Canonical column shapes ACCEPTED — exactly the backend
  // serialisation (str(Decimal) of numeric(18,2) / to_cents;
  // hand-computed oracles).
  expect(loanWith("balance", "0.00")).toBe(true);
  expect(loanWith("principal", "2000000.00")).toBe(true);
  expect(loanWith("penalty_due", "1500.10")).toBe(true);
  expect(rowWith("paid_amount", "0.00")).toBe(true);

  // Garbage shapes REJECTED on every money field — each previously
  // flowed into fmtKes unchallenged (bare z.string()).
  const garbage = ["abc", "1e5", "007.10", "1500.1", "1500.100", "1500", "1,500.10", " 1500.10", "NaN", ""];
  for (const value of garbage) {
    expect(loanWith("principal", value)).toBe(false);
    expect(loanWith("balance", value)).toBe(false);
    expect(loanWith("penalty_due", value)).toBe(false);
    expect(rowWith("principal_due", value)).toBe(false);
    expect(rowWith("total_due", value)).toBe(false);
  }

  // A '-' is a CONTRACT VIOLATION on every loans/loan_schedules money
  // column (CHECK > 0 / >= 0, migrations 0001 + 0007) and on the
  // settlement quote (domain settlement_quote REFUSES negatives).
  expect(loanWith("balance", "-1.00")).toBe(false);
  expect(rowWith("paid_amount", "-1.00")).toBe(false);

  const quote = {
    as_of: "2026-08-03",
    principal_balance: "1234567.10",
    interest_due: "10000.10",
    penalties_due: "1500.10",
    total: "1246067.30",
  };
  expect(loanSchemas.settlementQuoteSchema.safeParse(quote).success).toBe(true);
  expect(
    loanSchemas.settlementQuoteSchema.safeParse({ ...quote, total: "-1.00" }).success,
  ).toBe(false);
  expect(
    loanSchemas.settlementQuoteSchema.safeParse({ ...quote, interest_due: "1e5" }).success,
  ).toBe(false);

  // Portfolio aggregates: COALESCE(SUM(...), 0) legitimately emits the
  // bare "0" for an empty book — the aggregate shape (and ONLY it)
  // accepts that one extra value; garbage still rejects.
  const summary = {
    active_loans: 0,
    outstanding_balance: "0",
    npl_balance: "0",
    npl_ratio_pct: "0.00",
    par30_balance: "0",
    par30_ratio_pct: "0.00",
    provisions: "0",
    penalties_due: "0",
    by_classification: [
      { classification: "normal", count: 0, balance: "0", provisions: "0" },
    ],
  };
  expect(loanSchemas.portfolioSummarySchema.safeParse(summary).success).toBe(true);
  expect(
    loanSchemas.portfolioSummarySchema.safeParse({ ...summary, outstanding_balance: "150" })
      .success,
  ).toBe(false);
  expect(
    loanSchemas.portfolioSummarySchema.safeParse({ ...summary, provisions: "-1.00" }).success,
  ).toBe(false);
  expect(
    loanSchemas.portfolioSummarySchema.safeParse({ ...summary, npl_balance: "007.10" }).success,
  ).toBe(false);
});

test("GET /portfolio/summary + /loans/{id}/schedule parse through their boundaries (server aggregates only)", async () => {
  const summary = await loansApi.fetchPortfolioSummary();
  expect(summary.outstanding_balance).toBe("9876543.10");
  expect(summary.active_loans).toBe(12);

  const schedule = await loansApi.fetchLoanSchedule(LOAN_ID);
  expect(schedule[0]?.principal_due).toBe("50000.10");
  expect(schedule[0]?.paid_amount).toBe("0.00");

  for (const call of calls) {
    expect(call.headers.get("authorization")).toMatch(/^Bearer /);
    expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  }
});
