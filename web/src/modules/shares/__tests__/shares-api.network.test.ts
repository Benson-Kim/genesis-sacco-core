/**
 * @jest-environment node
 *
 * Network-layer proofs for the share-transfers API layer through the
 * REAL generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the corrections/recovery reference harnesses (issue #31 batch 10,
 * ledger (l)/(m)):
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL; ids ride the PATH; the transferee and the
 *   amount ride the request BODY — key-exact.
 * - APPROVAL body is EMPTY by contract (v1.1 rule 3: the checker
 *   approves the PERSISTED snapshot — smuggle anything in and the
 *   key-exact assertion fails); REJECTION pins {version, reason}
 *   exactly.
 * - The register GET carries cursor/limit as QUERY only; the opaque
 *   cursor echoes back verbatim.
 * - MONEY (blocker (a)): the amount travels as the typed decimal
 *   STRING verbatim; every response figure is asserted by the Zod
 *   boundary (numbers REJECTED, wrong scale REJECTED); extra keys
 *   are STRIPPED; a missing key REFUSES; the record's nullable legs
 *   accept null and NOTHING else off-shape.
 * - A 409 (snapshot drift / balance shortfall under the locks)
 *   surfaces as a typed ApiError from ONE request; a 422 surfaces
 *   CANONICAL field keys (W56-5).
 */

// Module scope (two global-script suites would collide under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MAKER_ID = "88888888-8888-8888-8888-888888888888";
const FROM_ID = "11111111-1111-1111-1111-111111111111";
const TO_ID = "33333333-3333-3333-3333-444444444444";
const TRANSFER_ID = "aaaabbbb-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let mutationStatus = 201;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const recordOut = {
  id: TRANSFER_ID,
  from_member_id: FROM_ID,
  to_member_id: TO_ID,
  amount: "5000.10",
  status: "pending",
  out_transaction_id: null,
  in_transaction_id: null,
  created_by: MAKER_ID,
  approved_by: null,
  decided_at: null,
  version: 1,
  created_at: "2026-08-07T09:00:00Z",
};

const approveOut = {
  transfer_id: TRANSFER_ID,
  out_txn_ref: "ST-OUT-000123",
  in_txn_ref: "ST-IN-000123",
  amount: "5000.10",
  from_balance_after: "15000.25",
  to_balance_after: "20000.35",
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
  if (path === `/members/${FROM_ID}/share-transfers` && request.method === "POST") {
    if (mutationStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-shortfall" });
    }
    if (mutationStatus === 422) {
      // FastAPI validation body — the loc head is stripped to the
      // CANONICAL field key by toApiError (W56-5).
      return json(422, {
        detail: [{ loc: ["body", "to_member_id"], msg: "Input should be a valid UUID" }],
      });
    }
    // An internal lock note smuggled onto the response is STRIPPED at
    // the Zod boundary — it can never reach a screen.
    return json(201, { ...recordOut, internal_lock_note: "ROW-LOCK-SECRET" });
  }
  if (path === "/share-transfers" && request.method === "GET") {
    return json(200, { items: [recordOut], next_cursor: "opaque-cursor-1" });
  }
  if (path === `/share-transfers/${TRANSFER_ID}` && request.method === "GET") {
    return json(200, recordOut);
  }
  if (path === `/share-transfers/${TRANSFER_ID}/approval` && request.method === "POST") {
    if (mutationStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-drift" });
    }
    return json(201, approveOut);
  }
  if (path === `/share-transfers/${TRANSFER_ID}/rejection` && request.method === "POST") {
    if (mutationStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-stale" });
    }
    return json(200, {
      ...recordOut,
      status: "rejected",
      approved_by: USER_ID,
      decided_at: "2026-08-07T10:00:00Z",
      version: 2,
    });
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

// The generated client captures globalThis.fetch at creation time —
// install the stub BEFORE requiring the modules under test.
globalThis.fetch = fetchStub as typeof globalThis.fetch;
process.env.NEXT_PUBLIC_TENANT_ID = TENANT;

// Node has no window: back the per-tab custody site with a Map (the
// real storage read/write path still runs) — reference-harness model.
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
const sharesApi = require("../api") as typeof import("../api");
const sharesSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  mutationStatus = 201;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("MAKER request: the transferring member rides the PATH; the body is EXACTLY {to_member_id, amount} with the typed decimal STRING verbatim; secrets ride as HEADERS; extra response keys are STRIPPED; the PENDING record parses with honest nulls", async () => {
  const record = await sharesApi.requestShareTransfer(FROM_ID, TO_ID, "5000.10", "key-req-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  const url = new URL(call.url);
  expect(url.pathname).toBe(`/members/${FROM_ID}/share-transfers`);
  expect(url.search).toBe("");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.headers.get("idempotency-key")).toBe("key-req-1");
  expect(call.url).not.toContain(REFRESH_VALUE);

  // KEY-EXACT body (falsifiable: smuggle a rate, date or version into
  // the API layer and this fails); the amount is the STRING verbatim
  // — a float can never start from here.
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual(["amount", "to_member_id"]);
  expect(body["to_member_id"]).toBe(TO_ID);
  expect(body["amount"]).toBe("5000.10");

  // The PENDING workflow record: server truth verbatim, honest nulls,
  // the smuggled internal field never parses through the boundary.
  expect(record.status).toBe("pending");
  expect(record.amount).toBe("5000.10");
  expect(record.out_transaction_id).toBeNull();
  expect(record.approved_by).toBeNull();
  expect(record.version).toBe(1);
  expect("internal_lock_note" in record).toBe(false);
});

test("register GET: cursor/limit ride the QUERY only (opaque cursor echoed verbatim); rows parse; next_cursor surfaces for the keyset walk", async () => {
  const first = await sharesApi.fetchShareTransfersPage(null);
  const withCursor = await sharesApi.fetchShareTransfersPage("opaque-cursor-1");
  expect(calls).toHaveLength(2);
  const firstUrl = new URL(calls[0]!.url);
  expect(firstUrl.pathname).toBe("/share-transfers");
  expect(firstUrl.searchParams.get("cursor")).toBeNull();
  expect(firstUrl.searchParams.get("limit")).toBe(String(sharesApi.REGISTER_PAGE_SIZE));
  const secondUrl = new URL(calls[1]!.url);
  expect(secondUrl.searchParams.get("cursor")).toBe("opaque-cursor-1");
  expect(first.items).toHaveLength(1);
  expect(first.items[0]?.id).toBe(TRANSFER_ID);
  expect(first.nextCursor).toBe("opaque-cursor-1");
  expect(withCursor.nextCursor).toBe("opaque-cursor-1");
});

test("CHECKER approval: the wire body is EMPTY by contract (v1.1 rule 3 — key-exact {}); the id rides the PATH; the result parses with the server's figures verbatim", async () => {
  const result = await sharesApi.approveShareTransfer(TRANSFER_ID, "key-approve-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe(`/share-transfers/${TRANSFER_ID}/approval`);
  expect(call.headers.get("idempotency-key")).toBe("key-approve-1");
  // EMPTY body — falsifiable: smuggle any figure/version into the API
  // layer and this fails.
  expect(JSON.parse(call.body ?? "null")).toEqual({});
  expect(result.out_txn_ref).toBe("ST-OUT-000123");
  expect(result.from_balance_after).toBe("15000.25");
  expect(result.to_balance_after).toBe("20000.35");
});

test("CHECKER rejection: the body is EXACTLY {version, reason} (version pinned on the wire; the rationale mandatory); the rejected record parses", async () => {
  const record = await sharesApi.rejectShareTransfer(
    TRANSFER_ID,
    1,
    "Amount keyed against the wrong member.",
    "key-reject-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/share-transfers/${TRANSFER_ID}/rejection`);
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual(["reason", "version"]);
  expect(body["version"]).toBe(1);
  expect(body["reason"]).toBe("Amount keyed against the wrong member.");
  expect(record.status).toBe("rejected");
  expect(record.approved_by).toBe(USER_ID);
  expect(record.version).toBe(2);
});

test("by-id GET parses the record; a 409 on approval (snapshot drift under the full lock set) surfaces as ONE typed ApiError from ONE request — never a silent success, never a retry", async () => {
  const record = await sharesApi.fetchShareTransfer(TRANSFER_ID);
  expect(record.id).toBe(TRANSFER_ID);

  mutationStatus = 409;
  const thrown = await sharesApi
    .approveShareTransfer(TRANSFER_ID, "key-approve-409")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  const apiError = thrown as InstanceType<typeof ApiError>;
  expect(apiError.status).toBe(409);
  expect(apiError.category).toBe("conflict");
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a 422 on the request surfaces field-level messages as ONE typed ApiError with CANONICAL field keys (W56-5)", async () => {
  mutationStatus = 422;
  const thrown = await sharesApi
    .requestShareTransfer(FROM_ID, TO_ID, "5000.10", "key-req-422")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  const apiError = thrown as InstanceType<typeof ApiError>;
  expect(apiError.status).toBe(422);
  expect(apiError.category).toBe("validation_error");
  expect(apiError.fields).toEqual([
    { field: "to_member_id", message: "Input should be a valid UUID" },
  ]);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("accept/reject matrix (hand-computed oracles): the workflow record — money shape, status vocabulary, nullable-never-optional legs, key exactness", () => {
  const parse = (record: Record<string, unknown>) =>
    sharesSchemas.shareTransferRecordSchema.safeParse(record).success;
  const withField = (field: string, value: unknown) =>
    parse({ ...recordOut, [field]: value });

  // Canonical accepted shapes.
  expect(parse(recordOut)).toBe(true);
  expect(withField("status", "posted")).toBe(true);
  expect(withField("status", "rejected")).toBe(true);
  expect(withField("amount", "0.01")).toBe(true);

  // Money garbage REJECTED — a number can never enter.
  for (const value of [5000.1, 5000, "5000.1", "5000.100", "5,000.10", "-1.00", "1e5", ""]) {
    expect(withField("amount", value)).toBe(false);
  }
  // Off-vocabulary status REJECTED (the 0040 CHECK vocabulary is the
  // contract).
  for (const value of ["PENDING", "approved", "void", "", 1, null]) {
    expect(withField("status", value)).toBe(false);
  }
  // Version: positive integer only.
  for (const value of [0, -1, 1.5, "1", null]) {
    expect(withField("version", value)).toBe(false);
  }

  // Nullable-never-optional legs: null ACCEPTED (honest history),
  // any other off-shape value REJECTED.
  for (const field of [
    "out_transaction_id",
    "in_transaction_id",
    "created_by",
    "approved_by",
    "decided_at",
  ]) {
    expect(withField(field, null)).toBe(true);
    expect(withField(field, 7)).toBe(false);
  }
  // Non-nullable keys refuse null.
  for (const field of ["id", "from_member_id", "to_member_id", "amount", "status", "created_at"]) {
    expect(withField(field, null)).toBe(false);
  }

  // KEY EXACTNESS: every key is REQUIRED — dropping any one is
  // contract drift and refuses to parse (falsifiable: soften a key to
  // .optional() and this leg fails).
  for (const key of Object.keys(recordOut)) {
    const missing: Record<string, unknown> = { ...recordOut };
    delete missing[key];
    expect(parse(missing)).toBe(false);
  }
});

test("accept/reject matrix: every ShareTransferOut money figure asserts the canonical str(Decimal) two-place shape; every key REQUIRED; no nullable leg exists", () => {
  const withField = (field: string, value: unknown) =>
    sharesSchemas.shareTransferResultSchema.safeParse({ ...approveOut, [field]: value }).success;

  expect(withField("amount", "5000.10")).toBe(true);
  expect(withField("from_balance_after", "0.00")).toBe(true);
  expect(withField("to_balance_after", "1000000000.00")).toBe(true);

  const moneyFields = ["amount", "from_balance_after", "to_balance_after"];
  const garbage = [5000.1, 5000, "5000.1", "5000.100", "5,000.10", "-1.00", "1e5", "NaN", ""];
  for (const field of moneyFields) {
    for (const value of garbage) {
      expect(withField(field, value)).toBe(false);
    }
  }

  const parse = (record: Record<string, unknown>) =>
    sharesSchemas.shareTransferResultSchema.safeParse(record).success;
  for (const key of Object.keys(approveOut)) {
    const missing: Record<string, unknown> = { ...approveOut };
    delete missing[key];
    expect(parse(missing)).toBe(false);
  }
  for (const key of Object.keys(approveOut)) {
    expect(withField(key, null)).toBe(false);
  }
});

test("entry pre-validation matrix (hand-computed oracles): UUID shape, amount shape (no leading zeros, ≤2dp, > 0), the self-transfer refusal and the mandatory rejection rationale — the layer above never even constructs a wire call from garbage", () => {
  const parse = (entry: { from_member_id: string; to_member_id: string; amount: string }) =>
    sharesSchemas.transferEntrySchema.safeParse(entry).success;

  expect(parse({ from_member_id: FROM_ID, to_member_id: TO_ID, amount: "5000.10" })).toBe(true);
  expect(parse({ from_member_id: FROM_ID, to_member_id: TO_ID, amount: "5000" })).toBe(true);
  // Self-transfer (case-insensitive) refused.
  expect(
    parse({ from_member_id: FROM_ID, to_member_id: FROM_ID.toUpperCase(), amount: "5000.10" }),
  ).toBe(false);
  // Amount shape.
  for (const bad of ["0", "0.00", "007.10", "5000.123", "-1.00", "5,000.10", "1e5", ""]) {
    expect(parse({ from_member_id: FROM_ID, to_member_id: TO_ID, amount: bad })).toBe(false);
  }
  // UUID shape.
  expect(parse({ from_member_id: "not-a-uuid", to_member_id: TO_ID, amount: "5000.10" })).toBe(
    false,
  );
  expect(parse({ from_member_id: FROM_ID, to_member_id: "nope", amount: "5000.10" })).toBe(false);

  // The checker's rationale: required, bounded (!52 F2).
  expect(sharesSchemas.rejectReasonEntrySchema.safeParse({ reason: "why" }).success).toBe(true);
  expect(sharesSchemas.rejectReasonEntrySchema.safeParse({ reason: "" }).success).toBe(false);
  expect(
    sharesSchemas.rejectReasonEntrySchema.safeParse({ reason: "x".repeat(501) }).success,
  ).toBe(false);
});
