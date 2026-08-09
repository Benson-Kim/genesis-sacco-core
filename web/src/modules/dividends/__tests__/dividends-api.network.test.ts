/**
 * @jest-environment node
 *
 * Network-layer proofs for the dividends API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the exits/transactions reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY: the opaque cursor is echoed back verbatim
 *   as a query parameter; no offset/page parameter exists (gate 1.3)
 *   and NO filter parameter exists at all (the P13.11 list contract
 *   declares none).
 * - NO money parameter can even be SENT (v1.1 rule 1: rates/period
 *   from tenant config, totals from the persisted snapshot;
 *   extra="forbid" server-side): every wire body is asserted
 *   key-by-key — declare and distribution carry exactly
 *   {batch_size: 200} (the SERVER's own documented default, never an
 *   operator choice), vote {vote}, void {version} and nothing else.
 * - Response money arrives as decimal STRINGS byte-identically; money
 *   as numbers, unknown statuses, garbage rates/dates/timestamps and
 *   the bare aggregate "0" are REJECTED at the Zod boundary (blocker
 *   (a) + the !63 F-R4 lesson); extra keys are STRIPPED.
 * - A 409 (FY slot taken / unconfigured tenant / snapshot drift) and
 *   a 422 (a smuggled rate refused by extra="forbid") each surface as
 *   a typed ApiError from ONE request (no transport-level retry).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const DECL_ID = "dddddddd-aaaa-bbbb-cccc-111111111111";
/** The declarer the server attributes (0020 requested_by). */
const DECLARER_ID = "99999999-9999-9999-9999-999999999999";

const calls: FetchCall[] = [];
let declareStatus = 201;
let distributionStatus = 200;
let listMoneyAsNumbers = false;
let listGarbageMoney = false;
let listUnknownStatus = false;
let listGarbageTimestamp = false;
let listGarbageFyDate = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const declarationOut = {
  id: DECL_ID,
  fy_start: "2025-07-01",
  fy_end: "2026-06-30",
  dividend_rate_pct: "14.00",
  rebate_rate_pct: "9.00",
  eligible_members: 42,
  total_share_basis: "500000.00",
  total_deposit_basis: "1200000.10",
  total_dividend: "70000.10",
  total_rebate: "108000.00",
  total_payout: "178000.10",
  status: "declared",
  // Declarer attribution (issue #31 ledger (a).4): the bare staff
  // UUID from the 0020 column — nullable, never optional.
  requested_by: DECLARER_ID,
  decided_at: null,
  distributed_at: null,
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

const runOut = {
  scanned: 42,
  claimed: 40,
  skipped_zero: 0,
  skipped_claimed: 0,
  unclaimed: 1,
  dividend_total: "70000.10",
  rebate_total: "108000.00",
  payout_total: "178000.10",
  pending_members: 1,
  batches: 1,
  status: "approved",
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
  if (path === "/dividends/declarations" && request.method === "GET") {
    if (listMoneyAsNumbers) {
      return json(200, {
        items: [{ ...declarationOut, total_payout: 178000.1 }],
        next_cursor: null,
      });
    }
    if (listGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the shared shape
      // assertion is the only thing standing between it and fmtKes.
      return json(200, {
        items: [{ ...declarationOut, total_rebate: "007.10" }],
        next_cursor: null,
      });
    }
    if (listUnknownStatus) {
      return json(200, { items: [{ ...declarationOut, status: "voided" }], next_cursor: null });
    }
    if (listGarbageTimestamp) {
      return json(200, {
        items: [{ ...declarationOut, created_at: "yesterday-ish" }],
        next_cursor: null,
      });
    }
    if (listGarbageFyDate) {
      return json(200, {
        items: [{ ...declarationOut, fy_end: "30/06/2026" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [declarationOut], next_cursor: "cursor-page-2" });
  }
  if (path === "/dividends/declarations" && request.method === "POST") {
    if (declareStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-fy" });
    }
    if (declareStatus === 422) {
      return json(422, {
        category: "validation",
        correlation_id: "corr-v",
        fields: { dividend_rate_pct: "extra fields forbidden" },
      });
    }
    return json(201, { ...declarationOut, internal_scan_note: "SCAN-SECRET" });
  }
  if (path === `/dividends/declarations/${DECL_ID}` && request.method === "GET") {
    return json(200, declarationOut);
  }
  if (path === `/dividends/declarations/${DECL_ID}/votes` && request.method === "POST") {
    return json(201, { approvals: 1, rejections: 0, decision: null, status: "declared" });
  }
  if (path === `/dividends/declarations/${DECL_ID}/void` && request.method === "POST") {
    return json(200, { ...declarationOut, status: "rejected", version: 2 });
  }
  if (path === `/dividends/declarations/${DECL_ID}/distribution` && request.method === "POST") {
    if (distributionStatus !== 200) {
      return json(distributionStatus, { category: "conflict", correlation_id: "corr-drift" });
    }
    return json(200, { ...runOut, internal_batch_trace: "TRACE-SECRET" });
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
const dividendsApi = require("../api") as typeof import("../api");
const dividendSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  declareStatus = 201;
  distributionStatus = 200;
  listMoneyAsNumbers = false;
  listGarbageMoney = false;
  listUnknownStatus = false;
  listGarbageTimestamp = false;
  listGarbageFyDate = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET /dividends/declarations: keyset params ONLY (no offset/page) and NO filter parameter at all; the opaque cursor echoes back VERBATIM", async () => {
  await dividendsApi.fetchDeclarationsPage(null);
  const page = await dividendsApi.fetchDeclarationsPage("cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/dividends/declarations");
  expect(first.searchParams.get("limit")).toBe(String(dividendsApi.DIVIDENDS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  // The P13.11 list contract declares NO filters — none are sent.
  expect([...first.searchParams.keys()].sort()).toEqual(["limit"]);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  // The boundary transform yields the client-side page shape verbatim —
  // trailing cents survive because the value never becomes a number.
  expect(page.items[0]?.total_payout).toBe("178000.10");
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("POST /dividends/declarations: the body is exactly {batch_size: 200} — the SERVER's own documented default; no rate, period, total or any operator value can even be sent; EXTRA response keys are STRIPPED", async () => {
  const record = await dividendsApi.declareDividend("key-declare-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/dividends/declarations");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-declare-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // KEY EXACTNESS (falsifiable: add a rate/period/total key to the
  // API layer — or make batch_size operator-chosen — and this fails).
  expect(Object.keys(body)).toEqual(["batch_size"]);
  expect(body["batch_size"]).toBe(dividendsApi.SERVER_DEFAULT_BATCH_SIZE);
  expect(body["batch_size"]).toBe(200);

  // The SERVER's snapshot comes back verbatim; the stubbed internal
  // field can never reach a screen (stripped at the boundary).
  expect(record.total_payout).toBe("178000.10");
  expect(record.dividend_rate_pct).toBe("14.00");
  expect("internal_scan_note" in record).toBe(false);
});

test("POST votes: the body is exactly {vote}; the tally parses verbatim (server counts, never client-derived)", async () => {
  const tally = await dividendsApi.voteOnDeclaration(DECL_ID, "approve", "key-vote-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/dividends/declarations/${DECL_ID}/votes`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-vote-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ vote: "approve" });
  expect(tally.approvals).toBe(1);
  expect(tally.rejections).toBe(0);
  expect(tally.decision).toBeNull();
  expect(tally.status).toBe("declared");
});

test("POST void: the body is exactly {version} pinning the acted-on record state (1.4)", async () => {
  const record = await dividendsApi.voidDeclaration(DECL_ID, 1, "key-void-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/dividends/declarations/${DECL_ID}/void`);
  expect(call.headers.get("idempotency-key")).toBe("key-void-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 1 });
  expect(record.status).toBe("rejected");
  expect(record.version).toBe(2);
});

test("POST distribution: the body is exactly {batch_size: 200} — no money field, no version; the run report parses verbatim incl. the issue-#19 unclaimed disposition count; EXTRA keys are STRIPPED", async () => {
  const run = await dividendsApi.distributeDeclaration(DECL_ID, "key-run-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/dividends/declarations/${DECL_ID}/distribution`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-run-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body)).toEqual(["batch_size"]);
  expect(body["batch_size"]).toBe(200);

  expect(run.claimed).toBe(40);
  expect(run.unclaimed).toBe(1);
  expect(run.pending_members).toBe(1);
  expect(run.payout_total).toBe("178000.10");
  expect(run.status).toBe("approved");
  expect("internal_batch_trace" in run).toBe(false);
});

test("a declare 409 (FY slot taken / unconfigured / zero payout) surfaces as ONE ApiError — no transport-level retry", async () => {
  declareStatus = 409;
  const thrown = await dividendsApi.declareDividend("key-declare-conflict").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a declare 422 (the extra=\"forbid\" refusal shape) surfaces as ONE typed ApiError with the server's field verdicts", async () => {
  declareStatus = 422;
  const thrown = await dividendsApi.declareDividend("key-declare-422").catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(422);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a distribution 409 (snapshot drift on first-run verification / concurrent void) surfaces as ONE ApiError — no replay", async () => {
  distributionStatus = 409;
  const thrown = await dividendsApi
    .distributeDeclaration(DECL_ID, "key-run-drift")
    .catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a contract-violating response (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  listMoneyAsNumbers = true;
  const thrown = await dividendsApi.fetchDeclarationsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("total_payout");
});

test("an unknown declaration status is a contract violation and is REJECTED — it can never arm a lifecycle write", async () => {
  listUnknownStatus = true;
  const thrown = await dividendsApi.fetchDeclarationsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("status");
});

test("a garbage timestamp is REJECTED at the boundary (the !63 F-R4 lesson)", async () => {
  listGarbageTimestamp = true;
  const thrown = await dividendsApi.fetchDeclarationsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("created_at");
});

test("a garbage FY date is REJECTED at the boundary — the FY bounds are date.isoformat() calendar dates, nothing else", async () => {
  listGarbageFyDate = true;
  const thrown = await dividendsApi.fetchDeclarationsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("fy_end");
});

test("a garbage money STRING is REJECTED at the wire boundary — a value that passes z.string() can still never reach fmtKes", async () => {
  listGarbageMoney = true;
  const thrown = await dividendsApi.fetchDeclarationsPage(null).catch((e: unknown) => e);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("total_rebate");
});

test("accept/reject matrix (hand-computed oracles): declaration money asserts the CANONICAL numeric(18,2) str(Decimal) shape; NO field on this contract has a negative branch; rates are two-place 0..100 column values; FY bounds are dates", () => {
  const declWith = (field: string, value: unknown) =>
    dividendSchemas.declarationSchema.safeParse({ ...declarationOut, [field]: value }).success;

  // Canonical shapes ACCEPTED — exactly the backend serialisation.
  expect(declWith("total_share_basis", "0.00")).toBe(true);
  expect(declWith("total_payout", "178000.10")).toBe(true);
  expect(declWith("total_dividend", "70000.10")).toBe(true);
  expect(declWith("dividend_rate_pct", "14.00")).toBe(true);
  expect(declWith("rebate_rate_pct", "0.00")).toBe(true);
  expect(declWith("fy_start", "2025-07-01")).toBe(true);

  // Garbage shapes REJECTED on every money field — none of these is a
  // str(Decimal) of a numeric(18,2) value.
  const garbage = [
    "abc",
    "1e5",
    "007.10", // leading zeros beyond a lone 0
    "178000.1", // wrong scale (the server always emits two places)
    "178000.100",
    "178000", // missing scale
    "178,000.10", // grouped — grouping is fmtKes's job, never the wire's
    " 178000.10",
    "178000.10 ",
    "NaN",
    "Infinity",
    "0x10",
    "",
  ];
  const moneyFields = [
    "total_share_basis",
    "total_deposit_basis",
    "total_dividend",
    "total_rebate",
    "total_payout",
  ];
  for (const field of moneyFields) {
    for (const value of garbage) {
      expect(declWith(field, value)).toBe(false);
    }
  }

  // A '-' is a CONTRACT VIOLATION on EVERY field of this contract:
  // all five money columns carry CHECK (>= 0) — total_payout CHECK
  // (> 0) — and both rates CHECK (0..100) (migration 0020). There is
  // no signedMoney field here, unlike ExitOut.net_payable.
  for (const field of [...moneyFields, "dividend_rate_pct", "rebate_rate_pct"]) {
    expect(declWith(field, "-1.00")).toBe(false);
  }

  // Rates: two-place numeric(5,2) shape — never a bare integer, never
  // a single decimal place, never a percent sign (rendering adds it).
  expect(declWith("dividend_rate_pct", "14")).toBe(false);
  expect(declWith("dividend_rate_pct", "14.5")).toBe(false);
  expect(declWith("dividend_rate_pct", "14%")).toBe(false);

  // FY bounds: date.isoformat() ONLY.
  expect(declWith("fy_end", "30/06/2026")).toBe(false);
  expect(declWith("fy_end", "2026-06-30T00:00:00")).toBe(false);
  expect(declWith("fy_end", "")).toBe(false);

  // eligible_members / version: integers, never numeric strings.
  expect(declWith("eligible_members", "42")).toBe(false);
  expect(declWith("version", "1")).toBe(false);
});

test("distribution-run matrix (hand-computed oracles): totals are ZERO-seeded scale-2 Decimal sums — \"0.00\" is the legitimate empty-run value and the bare aggregate \"0\" is a contract violation; counts are integers", () => {
  const runWith = (field: string, value: unknown) =>
    dividendSchemas.distributionRunSchema.safeParse({ ...runOut, [field]: value }).success;

  // The all-skipped run: domain/money.py ZERO = Decimal("0.00") seeds
  // every total, so the wire value keeps two places even with no rows
  // — unlike COALESCE(SUM(...), 0) aggregates elsewhere.
  expect(runWith("dividend_total", "0.00")).toBe(true);
  expect(runWith("rebate_total", "0.00")).toBe(true);
  expect(runWith("payout_total", "0.00")).toBe(true);
  expect(runWith("dividend_total", "0")).toBe(false);
  expect(runWith("payout_total", "0")).toBe(false);

  expect(runWith("payout_total", "-1.00")).toBe(false);
  expect(runWith("payout_total", "1e5")).toBe(false);
  expect(runWith("scanned", "42")).toBe(false);
  expect(runWith("unclaimed", "1")).toBe(false);
  expect(runWith("status", "escalated")).toBe(false);
});

test("timestamp nullability is exact: decided_at/distributed_at accept the honest NULL; created_at does NOT — and a MISSING key is a contract violation everywhere (key exactness)", () => {
  const parse = (record: Record<string, unknown>) =>
    dividendSchemas.declarationSchema.safeParse(record).success;

  expect(parse({ ...declarationOut, decided_at: null })).toBe(true);
  expect(parse({ ...declarationOut, distributed_at: null })).toBe(true);
  expect(parse({ ...declarationOut, created_at: null })).toBe(false);

  // KEY EXACTNESS: dropping any contract key is a violation — the
  // nullable fields are nullable, NOT optional (falsifiable: soften
  // the schema to .optional() and these legs fail).
  for (const key of Object.keys(declarationOut)) {
    const missing: Record<string, unknown> = { ...declarationOut };
    delete missing[key];
    expect(parse(missing)).toBe(false);
  }
});

test("declarer attribution matrix (issue #31 ledger (a).4 — hand-computed oracles): requested_by is the bare staff UUID string, NULLABLE-not-optional; a smuggled identity object or number never parses; server truth round-trips VERBATIM", async () => {
  const parse = (record: Record<string, unknown>) =>
    dividendSchemas.declarationSchema.safeParse(record).success;

  // The two honest server values: an attributed UUID and NULL (a
  // system-actor declaration) — never invented client-side.
  expect(parse({ ...declarationOut, requested_by: DECLARER_ID })).toBe(true);
  expect(parse({ ...declarationOut, requested_by: null })).toBe(true);
  // Nullable, NOT optional: a missing key is contract drift (also
  // covered by the exactness loop above — pinned here by name so the
  // (a).4 leg is falsifiable in isolation).
  const missing: Record<string, unknown> = { ...declarationOut };
  delete missing["requested_by"];
  expect(parse(missing)).toBe(false);
  // Least disclosure: the contract carries the BARE UUID — an
  // identity object or numeric id is a violation, never rendered.
  expect(parse({ ...declarationOut, requested_by: { id: DECLARER_ID, name: "Jane" } })).toBe(
    false,
  );
  expect(parse({ ...declarationOut, requested_by: 42 })).toBe(false);

  // Server truth round-trips verbatim through the real client + Zod
  // boundary on both the register and the record read.
  const page = await dividendsApi.fetchDeclarationsPage(null);
  expect(page.items[0]?.requested_by).toBe(DECLARER_ID);
  const record = await dividendsApi.fetchDeclaration(DECL_ID);
  expect(record.requested_by).toBe(DECLARER_ID);
});
