/**
 * @jest-environment node
 *
 * Network-layer proofs for the recovery API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the exits/transactions reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - Keyset pagination ONLY on the worklist and notes reads (gate 1.3):
 *   the opaque cursor echoes back VERBATIM; no offset/page parameter
 *   exists. The worklist sends ONLY the two DECLARED filter params
 *   (issue #31 ledger (a).3) — code-owned vocabulary values verbatim,
 *   "" meaning ABSENT (the no-filter request stays byte-identical to
 *   the as-built read); the exact wire key set is pinned.
 * - NO money parameter exists in ANY body, and NO timestamp can even
 *   be sent (addendum A7): every wire body is asserted KEY-BY-KEY —
 *   open {loan_id}, assign {version, assignee_id}, notes {note}, and
 *   the disposition carries ONLY the keys its target requires (!53
 *   F1/F2): pause {version, status, reason}, resume {version, status},
 *   restructure-close {version, status, note}.
 * - LEAST DISCLOSURE (addendum A5): no money key exists anywhere in
 *   the parse; garbage timestamps and unknown vocabulary are REJECTED
 *   at the Zod boundary (the !63 F-R4 lesson); extra keys STRIPPED.
 * - A 409 surfaces as a typed ApiError from ONE request; a 422
 *   surfaces field-level messages as a typed ApiError with CANONICAL
 *   field keys (W56-5).
 */

// Module scope (two global-script suites would collide under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const CASE_ID = "cccccccc-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let openStatus = 201;
let dispositionStatus = 200;
let worklistVariant: "canonical" | "money-smuggle" | "unknown-status" | "garbage-timestamp" =
  "canonical";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const caseOut = {
  id: CASE_ID,
  loan_id: LOAN_ID,
  status: "open",
  assignee_id: null,
  opened_by: OFFICER_ID,
  classification_at_open: "doubtful",
  days_past_due_at_open: 120,
  opened_at: "2026-08-01T09:00:00+00:00",
  first_assigned_at: null,
  closed_at: null,
  version: 2,
};

const worklistRowOut = {
  case_id: CASE_ID,
  loan_id: LOAN_ID,
  member_id: MEMBER_ID,
  days_past_due: 120,
  classification: "doubtful",
  assignee_id: null,
  assignee_unassignable: false,
  opened_at: "2026-08-01T09:00:00+00:00",
  first_assigned_at: null,
  version: 2,
};

const noteOut = {
  id: "dddddddd-1111-2222-3333-444444444444",
  author_id: OFFICER_ID,
  note: "Called the member — promised to pay Friday.",
  created_at: "2026-08-02T10:00:00+00:00",
  is_outcome: false,
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
  if (path === "/recovery-cases" && request.method === "GET") {
    if (worklistVariant === "money-smuggle") {
      // The A5 violation: a balance smuggled onto the worklist row —
      // STRIPPED at the boundary, never rendered.
      return json(200, {
        items: [{ ...worklistRowOut, balance: "40000.00" }],
        next_cursor: null,
      });
    }
    if (worklistVariant === "unknown-status") {
      return json(200, {
        items: [{ ...worklistRowOut, classification: "zombie" }],
        next_cursor: null,
      });
    }
    if (worklistVariant === "garbage-timestamp") {
      return json(200, {
        items: [{ ...worklistRowOut, opened_at: "yesterday-ish" }],
        next_cursor: null,
      });
    }
    return json(200, { items: [worklistRowOut], next_cursor: "cursor-page-2" });
  }
  if (path === "/recovery-cases" && request.method === "POST") {
    if (openStatus === 422) {
      // FastAPI validation body — the loc head is stripped to the
      // CANONICAL field key by toApiError (W56-5).
      return json(422, {
        detail: [{ loc: ["body", "loan_id"], msg: "Input should be a valid UUID" }],
      });
    }
    return json(201, { ...caseOut, internal_lock_note: "ROW-LOCK-SECRET" });
  }
  if (path === `/recovery-cases/${CASE_ID}` && request.method === "GET") {
    return json(200, caseOut);
  }
  if (path === `/recovery-cases/${CASE_ID}/assign` && request.method === "POST") {
    return json(200, {
      ...caseOut,
      assignee_id: OFFICER_ID,
      first_assigned_at: "2026-08-02T10:00:00+00:00",
      version: 3,
    });
  }
  if (path === `/recovery-cases/${CASE_ID}/disposition` && request.method === "POST") {
    if (dispositionStatus !== 200) {
      return json(dispositionStatus, { category: "conflict", correlation_id: "corr-gate" });
    }
    const sent = JSON.parse(body ?? "{}") as { status?: string };
    return json(200, { ...caseOut, status: sent.status ?? "open", version: 3 });
  }
  if (path === `/recovery-cases/${CASE_ID}/notes` && request.method === "POST") {
    return json(201, noteOut);
  }
  if (path === `/recovery-cases/${CASE_ID}/outcome-note` && request.method === "POST") {
    return json(201, { ...noteOut, is_outcome: true });
  }
  if (path === `/recovery-cases/${CASE_ID}/notes` && request.method === "GET") {
    return json(200, { items: [noteOut], next_cursor: "notes-cursor-2" });
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
const recoveryApi = require("../api") as typeof import("../api");
const recoverySchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  openStatus = 201;
  dispositionStatus = 200;
  worklistVariant = "canonical";
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

/** The default (no-filter) view — "" values send NOTHING. */
const NO_FILTERS = { status: "", classification: "" } as const;

test("GET /recovery-cases: keyset params ONLY; the no-filter request stays BYTE-IDENTICAL to the as-built read (no status/classification key on the wire); the opaque cursor echoes back VERBATIM; secrets ride as HEADERS", async () => {
  await recoveryApi.fetchWorklistPage(NO_FILTERS, null);
  const page = await recoveryApi.fetchWorklistPage(NO_FILTERS, "cursor-page-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/recovery-cases");
  expect(first.searchParams.get("limit")).toBe(String(recoveryApi.WORKLIST_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  // "" filters are ABSENT from the URL — the server's as-built default
  // (OPEN, most delinquent first) is requested byte-identically.
  expect(first.searchParams.has("status")).toBe(false);
  expect(first.searchParams.has("classification")).toBe(false);
  expect(first.searchParams.has("assignee")).toBe(false);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(second.searchParams.has("offset")).toBe(false);

  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);

  expect(page.items[0]?.days_past_due).toBe(120);
  expect(page.nextCursor).toBe("cursor-page-2");
});

test("GET /recovery-cases with the DECLARED filters (issue #31 ledger (a).3): status and classification travel as the code-owned vocabulary values VERBATIM, alongside keyset params ONLY — no undeclared key can reach the wire", async () => {
  await recoveryApi.fetchWorklistPage(
    { status: "irrecoverable_pending_write_off", classification: "loss" },
    "cursor-page-2",
  );
  expect(calls).toHaveLength(1);

  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe("/recovery-cases");
  // The two declared params, the vocabulary member verbatim (the
  // server binds it; an out-of-vocabulary value is its 422).
  expect(url.searchParams.get("status")).toBe("irrecoverable_pending_write_off");
  expect(url.searchParams.get("classification")).toBe("loss");
  // Keyset params preserved beside the filters; the EXACT key set is
  // pinned — an undeclared/invented parameter fails this test.
  expect(url.searchParams.get("cursor")).toBe("cursor-page-2");
  expect(url.searchParams.get("limit")).toBe(String(recoveryApi.WORKLIST_PAGE_SIZE));
  expect([...url.searchParams.keys()].sort()).toEqual([
    "classification",
    "cursor",
    "limit",
    "status",
  ]);
});

test("POST /recovery-cases: the body carries {loan_id} and NOTHING else — no timestamp, classification or DPD can even be sent (addendum A7); EXTRA response keys are STRIPPED", async () => {
  const record = await recoveryApi.openCase(LOAN_ID, "key-open-1");
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/recovery-cases");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-open-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // Key-exact: the ONE caller-chosen value — falsifiable: smuggle
  // opened_at or days_past_due into the API layer and this fails.
  expect(Object.keys(body)).toEqual(["loan_id"]);
  expect(body["loan_id"]).toBe(LOAN_ID);

  // The SERVER's snapshot verbatim; the stubbed internal field can
  // never reach a screen.
  expect(record.days_past_due_at_open).toBe(120);
  expect(record.classification_at_open).toBe("doubtful");
  expect("internal_lock_note" in record).toBe(false);
});

test("POST assign: the body is exactly {version, assignee_id} pinning the acted-on record state (1.4)", async () => {
  const record = await recoveryApi.assignCase(CASE_ID, 2, OFFICER_ID, "key-assign-1");
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe(`/recovery-cases/${CASE_ID}/assign`);
  expect(call.headers.get("idempotency-key")).toBe("key-assign-1");
  expect(JSON.parse(call.body ?? "null")).toEqual({ version: 2, assignee_id: OFFICER_ID });
  expect(record.assignee_id).toBe(OFFICER_ID);
  expect(record.version).toBe(3);
});

test("POST disposition — target-paired bodies (!53 F1/F2): pause sends {version, status, reason} with NO note key; resume sends NEITHER rationale key; restructure-close sends {version, status, note} with NO reason key", async () => {
  // Pause: the reason rides; the note key is NOT sent at all.
  await recoveryApi.setDisposition(
    CASE_ID,
    2,
    { status: "disputed", reason: "Member disputes the arrears", note: "" },
    "key-disp-1",
  );
  const pauseBody = JSON.parse(calls[0]!.body ?? "{}") as Record<string, unknown>;
  expect(pauseBody).toEqual({
    version: 2,
    status: "disputed",
    reason: "Member disputes the arrears",
  });
  expect(Object.keys(pauseBody).sort()).toEqual(["reason", "status", "version"]);

  // Resume: NEITHER rationale key is sent — a resume never smuggles a
  // reason or note (the server 422s a mismatched pairing).
  await recoveryApi.setDisposition(
    CASE_ID,
    2,
    { status: "open", reason: "left-over text", note: "left-over text" },
    "key-disp-2",
  );
  const resumeBody = JSON.parse(calls[1]!.body ?? "{}") as Record<string, unknown>;
  expect(resumeBody).toEqual({ version: 2, status: "open" });
  expect(Object.keys(resumeBody).sort()).toEqual(["status", "version"]);

  // Restructure-close: THE outcome note rides; the reason key does NOT.
  await recoveryApi.setDisposition(
    CASE_ID,
    2,
    { status: "closed_restructured", reason: "", note: "Rescheduled over 24 months." },
    "key-disp-3",
  );
  const closeBody = JSON.parse(calls[2]!.body ?? "{}") as Record<string, unknown>;
  expect(closeBody).toEqual({
    version: 2,
    status: "closed_restructured",
    note: "Rescheduled over 24 months.",
  });
  expect(Object.keys(closeBody).sort()).toEqual(["note", "status", "version"]);
});

test("POST notes / outcome-note: the body is exactly {note} — append-only rows; no edit/delete route even exists in the API layer", async () => {
  const appended = await recoveryApi.addCaseNote(CASE_ID, "Called, no answer", "key-note-1");
  expect(new URL(calls[0]!.url).pathname).toBe(`/recovery-cases/${CASE_ID}/notes`);
  expect(JSON.parse(calls[0]!.body ?? "null")).toEqual({ note: "Called, no answer" });
  expect(appended.is_outcome).toBe(false);

  const outcome = await recoveryApi.addOutcomeNote(CASE_ID, "Cured after June.", "key-out-1");
  expect(new URL(calls[1]!.url).pathname).toBe(`/recovery-cases/${CASE_ID}/outcome-note`);
  expect(JSON.parse(calls[1]!.body ?? "null")).toEqual({ note: "Cured after June." });
  expect(outcome.is_outcome).toBe(true);

  // Append-only at the LAYER level: no update/delete function exists —
  // the module exposes exactly what the contract has (addendum A2).
  const layer = recoveryApi as Record<string, unknown>;
  expect(layer["updateCaseNote"]).toBeUndefined();
  expect(layer["deleteCaseNote"]).toBeUndefined();
});

test("GET notes: keyset ONLY; the opaque cursor echoes back VERBATIM", async () => {
  await recoveryApi.fetchCaseNotesPage(CASE_ID, null);
  const page = await recoveryApi.fetchCaseNotesPage(CASE_ID, "notes-cursor-2");
  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe(`/recovery-cases/${CASE_ID}/notes`);
  expect(first.searchParams.get("limit")).toBe(String(recoveryApi.NOTES_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("notes-cursor-2");
  expect(page.items[0]?.author_id).toBe(OFFICER_ID);
  expect(page.nextCursor).toBe("notes-cursor-2");
});

test("an illegal move through the single gatekeeper (or version drift) surfaces as ONE 409 ApiError — no transport-level retry, no replay", async () => {
  dispositionStatus = 409;
  const thrown = await recoveryApi
    .setDisposition(
      CASE_ID,
      1,
      { status: "disputed", reason: "stale attempt", note: "" },
      "key-disp-stale",
    )
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a 422 surfaces field-level messages as ONE typed ApiError (canonical field keys, W56-5) — never a silent success", async () => {
  openStatus = 422;
  const thrown = await recoveryApi
    .openCase(LOAN_ID, "key-open-422")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  const apiError = thrown as InstanceType<typeof ApiError>;
  expect(apiError.status).toBe(422);
  expect(apiError.category).toBe("validation_error");
  expect(apiError.fields).toEqual([{ field: "loan_id", message: "Input should be a valid UUID" }]);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("LEAST DISCLOSURE (addendum A5): a money key smuggled onto a worklist row is STRIPPED at the boundary — no money field exists in the parsed shape", async () => {
  worklistVariant = "money-smuggle";
  const page = await recoveryApi.fetchWorklistPage(NO_FILTERS, null);
  const row = page.items[0]!;
  // The A5 posture: the stripped row carries workflow facts only.
  expect("balance" in row).toBe(false);
  expect(Object.keys(row).sort()).toEqual([
    "assignee_id",
    "assignee_unassignable",
    "case_id",
    "classification",
    "days_past_due",
    "first_assigned_at",
    "loan_id",
    "member_id",
    "opened_at",
    "version",
  ]);
});

test("unknown worklist classification is a contract violation and is REJECTED — it can never render a pill", async () => {
  worklistVariant = "unknown-status";
  const thrown = await recoveryApi
    .fetchWorklistPage(NO_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("classification");
});

test("a garbage SLA timestamp is REJECTED at the boundary (the !63 F-R4 lesson) — 'Invalid Date' can never render on the case record", async () => {
  worklistVariant = "garbage-timestamp";
  const thrown = await recoveryApi
    .fetchWorklistPage(NO_FILTERS, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("opened_at");
});

test("case-record accept/reject matrix (hand-computed oracles): status vocabulary is the 0033 six-state machine; open classification is the NPL subset; attribution keys are exact (opened_by required, assignee_id nullable-not-optional)", async () => {
  const record = await recoveryApi.fetchCase(CASE_ID);
  expect(record.opened_by).toBe(OFFICER_ID);
  expect(record.assignee_id).toBeNull();
  expect("full_name" in record).toBe(false);
  expect("email" in record).toBe(false);

  const withField = (mutate: (draft: Record<string, unknown>) => void) => {
    const draft: Record<string, unknown> = { ...caseOut };
    mutate(draft);
    return recoverySchemas.caseSchema.safeParse(draft).success;
  };

  // The six legitimate statuses parse; anything else refuses.
  for (const status of [
    "open",
    "irrecoverable_pending_write_off",
    "disputed",
    "closed_cured",
    "closed_written_off",
    "closed_restructured",
  ]) {
    expect(withField((draft) => (draft["status"] = status))).toBe(true);
  }
  expect(withField((draft) => (draft["status"] = "escalated"))).toBe(false);

  // A case can only OPEN at an NPL class (0026 CHECK: > 90 DPD).
  for (const cls of ["substandard", "doubtful", "loss"]) {
    expect(withField((draft) => (draft["classification_at_open"] = cls))).toBe(true);
  }
  expect(withField((draft) => (draft["classification_at_open"] = "normal"))).toBe(false);
  expect(withField((draft) => (draft["classification_at_open"] = "watch"))).toBe(false);

  // opened_by: REQUIRED server truth — null and missing both refuse.
  expect(withField((draft) => (draft["opened_by"] = null))).toBe(false);
  expect(withField((draft) => delete draft["opened_by"])).toBe(false);
  // assignee_id: nullable, NOT optional.
  expect(withField((draft) => (draft["assignee_id"] = OFFICER_ID))).toBe(true);
  expect(withField((draft) => (draft["assignee_id"] = null))).toBe(true);
  expect(withField((draft) => delete draft["assignee_id"])).toBe(false);
  // A smuggled identity object never parses.
  expect(
    withField((draft) => (draft["opened_by"] = { id: OFFICER_ID, full_name: "Jane" })),
  ).toBe(false);

  // days_past_due_at_open is an integer COUNT, never a decimal string.
  expect(withField((draft) => (draft["days_past_due_at_open"] = "120.00"))).toBe(false);
  expect(withField((draft) => (draft["days_past_due_at_open"] = 120.5))).toBe(false);
});
