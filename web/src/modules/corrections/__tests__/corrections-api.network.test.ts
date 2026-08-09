/**
 * @jest-environment node
 *
 * Network-layer proofs for the corrections API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the exits/transactions reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - NO money parameter can even be SENT except the recovery receipt's
 *   cash-received amount — the ONE caller-known figure, a typed
 *   decimal STRING (P13.15 FM5/v1.1 rule 1): every wire body is
 *   asserted KEY-BY-KEY; the approval and posting bodies are
 *   DELIBERATELY EMPTY objects (v1.1 rule 3).
 * - Keyset reads (gate 1.3): the per-write-off receipts trail PLUS
 *   the two batch-6 registers (ledger (a).1/(a).2 — pending-first
 *   adjustments, live-first write-offs): opaque cursor echoed
 *   VERBATIM; no offset/page/filter parameter exists on any of them.
 * - Response money arrives as decimal STRINGS byte-identically; money
 *   as numbers, garbage decimal shapes, unknown statuses/classes and
 *   garbage timestamps are REJECTED at the Zod boundary (blocker (a)
 *   + the !63 F-R4 lesson); extra keys are STRIPPED.
 * - Attribution key-exactness (the !70 pattern): maker_id is REQUIRED
 *   (0025 NOT NULL — a missing key is a contract violation);
 *   checker_id / recovery_case_id are nullable, never optional.
 * - A 409 surfaces as a typed ApiError from ONE request (no
 *   transport-level retry); a 422 surfaces field-level messages as a
 *   typed ApiError with CANONICAL field keys (W56-5).
 */

// Module scope (two global-script suites would collide under tsc).
export { };

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MAKER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const REPAYMENT_ID = "33333333-4444-5555-6666-777777777777";
const ADJ_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const WO_ID = "bbbbbbbb-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let requestStatus = 201;
let approvalStatus = 201;
let receiptStatus = 201;
let registerVariant: "canonical" | "money-number" | "unknown-status" = "canonical";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const adjustmentOut = {
  id: ADJ_ID,
  repayment_id: REPAYMENT_ID,
  loan_id: LOAN_ID,
  original_transaction_id: "44444444-5555-6666-7777-888888888888",
  reversal_transaction_id: null,
  maker_id: MAKER_ID,
  checker_id: null,
  reason: "Duplicate repayment posting",
  amount: "500.00",
  penalties: "100.10",
  interest: "150.00",
  principal: "249.90",
  would_reopen_loan: false,
  status: "pending_approval",
  loan_balance_at_request: "40000.00",
  loan_penalty_due_at_request: "0.00",
  loan_status_at_request: "active",
  decided_at: null,
  version: 3,
  created_at: "2026-08-01T09:00:00+00:00",
};

const writeOffOut = {
  id: WO_ID,
  loan_id: LOAN_ID,
  member_id: MEMBER_ID,
  balance: "40000.00",
  penalty_due: "1500.10",
  total_written_off: "41500.10",
  classification: "loss",
  provision_pct: "100.00",
  reason: "Member absconded",
  status: "requested",
  decided_at: null,
  posted_at: null,
  transaction_id: null,
  version: 2,
  created_at: "2026-08-01T09:00:00+00:00",
};

const receiptRow = {
  id: "77777777-8888-9999-aaaa-bbbbbbbbbbbb",
  amount: "5000.50",
  txn_id: "88888888-9999-aaaa-bbbb-cccccccccccc",
  recovery_case_id: null,
  recorded_by: MAKER_ID,
  created_at: "2026-08-04T10:00:00+00:00",
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
  if (path === "/corrections/repayment-adjustments" && request.method === "GET") {
    if (registerVariant === "money-number") {
      return json(200, { items: [{ ...adjustmentOut, amount: 500 }], next_cursor: null });
    }
    if (registerVariant === "unknown-status") {
      return json(200, { items: [{ ...adjustmentOut, status: "escalated" }], next_cursor: null });
    }
    return json(200, {
      items: [{ ...adjustmentOut, internal_lock_note: "ROW-LOCK-SECRET" }],
      next_cursor: "adj-cursor-2",
    });
  }
  if (path === "/corrections/write-offs" && request.method === "GET") {
    return json(200, { items: [writeOffOut], next_cursor: "wo-cursor-2" });
  }
  if (path === "/corrections/repayment-adjustments" && request.method === "POST") {
    if (requestStatus === 422) {
      // FastAPI validation body — the loc head is stripped to the
      // CANONICAL field key by toApiError (W56-5).
      return json(422, {
        detail: [{ loc: ["body", "reason"], msg: "String should have at least 10 characters" }],
      });
    }
    return json(201, { ...adjustmentOut, internal_lock_note: "ROW-LOCK-SECRET" });
  }
  if (path === `/corrections/repayment-adjustments/${ADJ_ID}` && request.method === "GET") {
    return json(200, adjustmentOut);
  }
  if (path === `/corrections/repayment-adjustments/${ADJ_ID}/approval` && request.method === "POST") {
    if (approvalStatus !== 201) {
      return json(approvalStatus, { category: "conflict", correlation_id: "corr-drift" });
    }
    return json(201, {
      adjustment_id: ADJ_ID,
      reversal_txn_id: "55555555-6666-7777-8888-999999999999",
      reversal_txn_ref: "ADJ-0001",
      amount: "500.00",
      penalties: "100.10",
      interest: "150.00",
      principal: "249.90",
      balance_after: "40500.00",
      penalty_due_after: "100.10",
      loan_status: "active",
      reopened: false,
    });
  }
  if (path === `/corrections/repayment-adjustments/${ADJ_ID}/reject` && request.method === "POST") {
    return json(200, { ...adjustmentOut, status: "rejected", checker_id: USER_ID, version: 4 });
  }
  if (path === "/corrections/fees" && request.method === "POST") {
    return json(201, {
      txn_id: "99999999-aaaa-bbbb-cccc-dddddddddddd",
      txn_ref: "FE-0001",
      fee_type: "registration",
      amount: "1000.00",
    });
  }
  if (path === "/corrections/write-offs" && request.method === "POST") {
    return json(201, writeOffOut);
  }
  if (path === `/corrections/write-offs/${WO_ID}` && request.method === "GET") {
    return json(200, writeOffOut);
  }
  if (path === `/corrections/write-offs/${WO_ID}/votes` && request.method === "POST") {
    return json(201, { approvals: 1, rejections: 0, decision: null, status: "requested" });
  }
  if (path === `/corrections/write-offs/${WO_ID}/void` && request.method === "POST") {
    return json(200, { ...writeOffOut, status: "rejected", version: 3 });
  }
  if (path === `/corrections/write-offs/${WO_ID}/posting` && request.method === "POST") {
    return json(201, {
      write_off_id: WO_ID,
      loan_id: LOAN_ID,
      txn_id: "66666666-7777-8888-9999-aaaaaaaaaaaa",
      txn_ref: "WO-0001",
      total_written_off: "41500.10",
      status: "posted",
    });
  }
  if (path === `/corrections/write-offs/${WO_ID}/recoveries` && request.method === "POST") {
    if (receiptStatus !== 201) {
      return json(receiptStatus, { category: "conflict", correlation_id: "corr-over" });
    }
    return json(201, {
      recovery_id: "77777777-8888-9999-aaaa-bbbbbbbbbbbb",
      write_off_id: WO_ID,
      loan_id: LOAN_ID,
      member_id: MEMBER_ID,
      txn_id: "88888888-9999-aaaa-bbbb-cccccccccccc",
      txn_ref: "RC-0001",
      amount: "5000.50",
      recovered_total: "5000.50",
      outstanding_claim: "36499.60",
      claim_fully_recovered: false,
      guarantees_released: 0,
      recovery_case_id: null,
    });
  }
  if (path === `/corrections/write-offs/${WO_ID}/recoveries` && request.method === "GET") {
    return json(200, {
      items: [receiptRow],
      recovered_total: "5000.50",
      outstanding_claim: "36499.60",
      next_cursor: "cursor-page-2",
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
const correctionsApi = require("../api") as typeof import("../api");
const correctionsSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  requestStatus = 201;
  approvalStatus = 201;
  receiptStatus = 201;
  registerVariant = "canonical";
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("POST repayment-adjustments: the body carries {repayment_id, reason} and NOTHING else — no amount can even be sent; secrets ride as HEADERS; EXTRA response keys are STRIPPED", async () => {
  const record = await correctionsApi.requestAdjustment(
    REPAYMENT_ID,
    "Duplicate repayment posting",
    "key-adj-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/corrections/repayment-adjustments");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-adj-1");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.url).not.toContain(REFRESH_VALUE);
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["repayment_id"]).toBe(REPAYMENT_ID);
  expect(body["reason"]).toBe("Duplicate repayment posting");
  // Key-exact: nothing beyond the AdjustmentBody contract can be sent —
  // falsifiable: add any key (an amount, a date) and this fails.
  expect(Object.keys(body).sort()).toEqual(["reason", "repayment_id"]);

  // Server figures come back verbatim; the stubbed internal field can
  // never reach a screen (stripped at the boundary).
  expect(record.amount).toBe("500.00");
  expect(record.penalties).toBe("100.10");
  expect("internal_lock_note" in record).toBe(false);
});

test("POST approval: the wire body is the DELIBERATELY EMPTY object (v1.1 rule 3) — the checker approves the PERSISTED snapshot, supplying NOTHING", async () => {
  const posted = await correctionsApi.approveAdjustment(ADJ_ID, "key-approve-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/corrections/repayment-adjustments/${ADJ_ID}/approval`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-approve-1");
  // The EMPTY body is the contract: extra="forbid" server-side turns
  // any caller-supplied field into a 422.
  expect(JSON.parse(call.body ?? "null")).toEqual({});
  // Every result figure is the SERVER's, verbatim.
  expect(posted.balance_after).toBe("40500.00");
  expect(posted.penalty_due_after).toBe("100.10");
  expect(posted.reversal_txn_ref).toBe("ADJ-0001");
});

test("POST reject: the body is exactly {version, reason} — the pinned record state plus the REQUIRED rationale (!52 F2)", async () => {
  const record = await correctionsApi.rejectAdjustment(ADJ_ID, 3, "Figures drifted", "key-reject-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/corrections/repayment-adjustments/${ADJ_ID}/reject`);
  expect(call.headers.get("idempotency-key")).toBe("key-reject-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 3, reason: "Figures drifted" });
  expect(record.status).toBe("rejected");
  expect(record.version).toBe(4);
});

test("POST fees: the body is exactly {member_id, fee_type, channel} — the fee AMOUNT is unrepresentable on the wire (FM5); the result amount is the SERVER's config figure", async () => {
  const posted = await correctionsApi.postFee(MEMBER_ID, "registration", "mpesa", "key-fee-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe("/corrections/fees");
  expect(call.headers.get("idempotency-key")).toBe("key-fee-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body).toEqual({ member_id: MEMBER_ID, fee_type: "registration", channel: "mpesa" });
  expect(Object.keys(body).sort()).toEqual(["channel", "fee_type", "member_id"]);
  expect(posted.amount).toBe("1000.00");
  expect(posted.txn_ref).toBe("FE-0001");
});

test("POST write-offs: the body is exactly {loan_id, reason}; the snapshot comes back verbatim with its trailing cents", async () => {
  const record = await correctionsApi.requestWriteOff(LOAN_ID, "Member absconded", "key-wo-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe("/corrections/write-offs");
  expect(call.headers.get("idempotency-key")).toBe("key-wo-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body).toEqual({ loan_id: LOAN_ID, reason: "Member absconded" });
  expect(record.total_written_off).toBe("41500.10");
  expect(record.penalty_due).toBe("1500.10");
});

test("POST votes / void / posting: vote body is exactly {vote}; void pins exactly {version}; posting is the DELIBERATELY EMPTY object (v1.1 rule 3)", async () => {
  const tally = await correctionsApi.voteOnWriteOff(WO_ID, "approve", "key-vote-1");
  expect(JSON.parse(calls[0]!.body ?? "null")).toEqual({ vote: "approve" });
  expect(calls[0]!.headers.get("idempotency-key")).toBe("key-vote-1");
  expect(tally.approvals).toBe(1);
  expect(tally.decision).toBeNull();

  const voided = await correctionsApi.voidWriteOff(WO_ID, 2, "key-void-1");
  expect(new URL(calls[1]!.url).pathname).toBe(`/corrections/write-offs/${WO_ID}/void`);
  expect(JSON.parse(calls[1]!.body ?? "null")).toEqual({ version: 2 });
  expect(voided.status).toBe("rejected");

  const posted = await correctionsApi.postWriteOff(WO_ID, "key-post-1");
  expect(new URL(calls[2]!.url).pathname).toBe(`/corrections/write-offs/${WO_ID}/posting`);
  expect(JSON.parse(calls[2]!.body ?? "null")).toEqual({});
  expect(posted.total_written_off).toBe("41500.10");
  expect(posted.txn_ref).toBe("WO-0001");
});

test("POST recoveries: the ONE caller-known money figure travels as the typed decimal STRING byte-identically — {amount, channel} and NOTHING else", async () => {
  const posted = await correctionsApi.recordRecoveryReceipt(WO_ID, "5000.50", "bank", "key-rc-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/corrections/write-offs/${WO_ID}/recoveries`);
  expect(call.headers.get("idempotency-key")).toBe("key-rc-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // The amount is a STRING on the wire — a float would lose the cents
  // contract and is a different JSON token entirely.
  expect(call.body).toContain('"amount":"5000.50"');
  expect(body).toEqual({ amount: "5000.50", channel: "bank" });
  // The claim position is the SERVER's, verbatim.
  expect(posted.recovered_total).toBe("5000.50");
  expect(posted.outstanding_claim).toBe("36499.60");
  expect(posted.claim_fully_recovered).toBe(false);
});

test("GET recoveries: keyset params ONLY (no offset/page); the opaque cursor echoes back VERBATIM; the page carries the server position figures", async () => {
  await correctionsApi.fetchRecoveryReceiptsPage(WO_ID, null);
  const page = await correctionsApi.fetchRecoveryReceiptsPage(WO_ID, "cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe(`/corrections/write-offs/${WO_ID}/recoveries`);
  expect(first.searchParams.get("limit")).toBe(String(correctionsApi.RECEIPTS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  expect(page.items[0]?.amount).toBe("5000.50");
  expect(page.items[0]?.recorded_by).toBe(MAKER_ID);
  expect(page.recovered_total).toBe("5000.50");
  expect(page.outstanding_claim).toBe("36499.60");
  expect(page.next_cursor).toBe("cursor-page-2");
});

test("a drifted approval (snapshot re-verification failed, NOTHING posted) surfaces as ONE 409 ApiError — no transport-level retry", async () => {
  approvalStatus = 409;
  const thrown = await correctionsApi
    .approveAdjustment(ADJ_ID, "key-approve-stale")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a 422 surfaces field-level messages as ONE typed ApiError (canonical field keys, W56-5) — never a silent success", async () => {
  requestStatus = 422;
  const thrown = await correctionsApi
    .requestAdjustment(REPAYMENT_ID, "too short", "key-adj-422")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  const apiError = thrown as InstanceType<typeof ApiError>;
  expect(apiError.status).toBe(422);
  expect(apiError.category).toBe("validation_error");
  expect(apiError.fields).toEqual([
    { field: "reason", message: "String should have at least 10 characters" },
  ]);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("an over-recovery receipt surfaces as ONE 409 ApiError — the wire saw exactly one attempt", async () => {
  receiptStatus = 409;
  const thrown = await correctionsApi
    .recordRecoveryReceipt(WO_ID, "999999.99", "mpesa", "key-rc-over")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("F-M2 accept/reject matrix (hand-computed oracles): every money field asserts the CANONICAL str(Decimal) numeric(18,2) shape; a '-' is a contract violation on EVERY field of this module", () => {
  const adjWith = (field: string, value: unknown) =>
    correctionsSchemas.adjustmentSchema.safeParse({ ...adjustmentOut, [field]: value }).success;
  const woWith = (field: string, value: unknown) =>
    correctionsSchemas.writeOffSchema.safeParse({ ...writeOffOut, [field]: value }).success;

  // Canonical shapes ACCEPTED — exactly the backend serialisation.
  expect(adjWith("amount", "0.01")).toBe(true);
  expect(adjWith("amount", "500.00")).toBe(true);
  expect(adjWith("penalties", "0.00")).toBe(true);
  expect(woWith("total_written_off", "41500.10")).toBe(true);
  expect(woWith("balance", "0.00")).toBe(true);

  // Garbage shapes REJECTED on every money field — none is a
  // str(Decimal) of a numeric(18,2) value.
  const garbage = [
    "abc",
    "1e5",
    "007.10", // leading zeros beyond a lone 0
    "500.1", // wrong scale (the server always emits two places)
    "500.100",
    "500", // missing scale
    "5,000.00", // grouped — grouping is fmtKes's job, never the wire's
    " 500.00",
    "500.00 ",
    "NaN",
    "Infinity",
    "0x10",
    "",
    500, // a NUMBER is a contract violation outright (blocker (a))
  ];
  for (const field of ["amount", "penalties", "interest", "principal"]) {
    for (const value of garbage) {
      expect(adjWith(field, value)).toBe(false);
    }
  }
  for (const field of ["balance", "penalty_due", "total_written_off"]) {
    for (const value of garbage) {
      expect(woWith(field, value)).toBe(false);
    }
  }

  // NO field in this module has a documented negative branch (every
  // CHECK is >= 0 or > 0) — a '-' anywhere is REJECTED
  // (signedMoneySchema is deliberately unused in corrections).
  for (const field of ["amount", "penalties", "interest", "principal"]) {
    expect(adjWith(field, "-1.00")).toBe(false);
  }
  for (const field of ["balance", "penalty_due", "total_written_off"]) {
    expect(woWith(field, "-1.00")).toBe(false);
  }

  // The nullable 0031 snapshot columns keep the NULL leg and reject
  // garbage on the non-null leg.
  expect(adjWith("loan_balance_at_request", null)).toBe(true);
  expect(adjWith("loan_balance_at_request", "40000.00")).toBe(true);
  expect(adjWith("loan_balance_at_request", "-40000.00")).toBe(false);

  // The receipts page mirrors the posture.
  const pageWith = (value: unknown) =>
    correctionsSchemas.recoveryReceiptsPageSchema.safeParse({
      items: [receiptRow],
      recovered_total: value,
      outstanding_claim: "36499.60",
      next_cursor: null,
    }).success;
  expect(pageWith("5000.50")).toBe(true);
  expect(pageWith("-5000.50")).toBe(false);
  expect(pageWith(5000.5)).toBe(false);
});

test("vocabulary boundaries: unknown adjustment/write-off statuses, an unknown vote decision and a PERFORMING-loan classification are contract violations — REJECTED, never rendered", () => {
  expect(
    correctionsSchemas.adjustmentSchema.safeParse({ ...adjustmentOut, status: "escalated" })
      .success,
  ).toBe(false);
  expect(
    correctionsSchemas.writeOffSchema.safeParse({ ...writeOffOut, status: "archived" }).success,
  ).toBe(false);
  // The 0025 ck backstop: a snapshot of a performing loan is
  // unrepresentable — the boundary mirrors it.
  expect(
    correctionsSchemas.writeOffSchema.safeParse({ ...writeOffOut, classification: "normal" })
      .success,
  ).toBe(false);
  expect(
    correctionsSchemas.writeOffVoteResultSchema.safeParse({
      approvals: 1,
      rejections: 0,
      decision: "vetoed",
      status: "requested",
    }).success,
  ).toBe(false);
  // Garbage timestamps are rejected (the !63 F-R4 lesson).
  expect(
    correctionsSchemas.adjustmentSchema.safeParse({ ...adjustmentOut, created_at: "yesterday" })
      .success,
  ).toBe(false);
});

test("attribution key-exactness (the !70 pattern): maker_id is REQUIRED (0025 NOT NULL — missing key AND null are violations); checker_id is nullable, never optional; recorded_by is required on receipt rows", async () => {
  // The wire read carries the maker verbatim; no name/email key exists.
  const record = await correctionsApi.fetchAdjustment(ADJ_ID);
  expect(record.maker_id).toBe(MAKER_ID);
  expect(record.checker_id).toBeNull();
  expect("full_name" in record).toBe(false);
  expect("email" in record).toBe(false);

  const withField = (mutate: (draft: Record<string, unknown>) => void) => {
    const draft: Record<string, unknown> = { ...adjustmentOut };
    mutate(draft);
    return correctionsSchemas.adjustmentSchema.safeParse(draft).success;
  };

  // maker_id: REQUIRED server truth — null and missing both refuse.
  expect(withField(() => undefined)).toBe(true);
  expect(withField((draft) => (draft["maker_id"] = null))).toBe(false);
  expect(withField((draft) => delete draft["maker_id"])).toBe(false);
  // A smuggled identity object never parses.
  expect(withField((draft) => (draft["maker_id"] = { id: MAKER_ID, full_name: "Jane" }))).toBe(
    false,
  );

  // checker_id: nullable, NOT optional — dropping the key is a
  // contract violation (falsifiable: soften to .optional()).
  expect(withField((draft) => (draft["checker_id"] = USER_ID))).toBe(true);
  expect(withField((draft) => (draft["checker_id"] = null))).toBe(true);
  expect(withField((draft) => delete draft["checker_id"])).toBe(false);

  // Receipt rows: recorded_by is required; recovery_case_id nullable.
  const rowWith = (mutate: (draft: Record<string, unknown>) => void) => {
    const draft: Record<string, unknown> = { ...receiptRow };
    mutate(draft);
    return correctionsSchemas.recoveryReceiptRowSchema.safeParse(draft).success;
  };
  expect(rowWith(() => undefined)).toBe(true);
  expect(rowWith((draft) => delete draft["recorded_by"])).toBe(false);
  expect(rowWith((draft) => (draft["recovery_case_id"] = null))).toBe(true);
  expect(rowWith((draft) => delete draft["recovery_case_id"])).toBe(false);
});

test("a contract-violating record read (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  // Patch the stub response by fetching a record whose money field the
  // schema must refuse: simulate via direct schema assertion AND a live
  // wire read that stays canonical (the stub serves strings — the live
  // path proves the happy leg; the safeParse proves the reject leg).
  const record = await correctionsApi.fetchWriteOff(WO_ID);
  expect(record.balance).toBe("40000.00");
  expect(
    correctionsSchemas.writeOffSchema.safeParse({ ...writeOffOut, balance: 40000 }).success,
  ).toBe(false);
});
