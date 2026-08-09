/**
 * @jest-environment node
 *
 * Network-layer proofs for the reports API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the transactions/loans reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a hand-built URL (the download capability token is the
 *   CONTRACT's own path parameter, serialized by the generated client).
 * - The export request body carries the report name and its PROVIDED
 *   id/date filters ONLY — empty filters are omitted entirely, no
 *   format/limit/column/storage key can even be sent (P13 blocker (a);
 *   extra="forbid" server-side).
 * - The queue drain sends NO body — the contract defines none.
 * - Contract-violating responses (malformed download paths, fractional
 *   row counts) are REJECTED at the Zod boundary; extra keys are
 *   STRIPPED (including the picker rows' settlement figures, which
 *   this module never renders).
 * - Picker reads are keyset ONLY: opaque cursor echoed verbatim, no
 *   offset/page parameter exists (gate 1.3).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXPORT_ID = "eeeeeeee-1111-2222-3333-444444444444";
const EXIT_ID = "dddddddd-1111-2222-3333-444444444444";
const DECLARATION_ID = "cccccccc-1111-2222-3333-444444444444";

const CSV_TOKEN = "csv-Tok_en-1";
const PDF_TOKEN = "pdf-Tok_en-1";

const calls: FetchCall[] = [];
let exportArtifactShape: "ok" | "hostile_path" | "fractional_rows" = "ok";
let exportRecordShape: "ok" | "garbage_timestamp" = "ok";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function artifactOut(): Record<string, unknown> {
  if (exportArtifactShape === "hostile_path") {
    // A poisoned download path pointing outside the export capability
    // namespace must be REJECTED, never followed.
    return {
      row_count: 3,
      truncated: false,
      row_limit: 10000,
      expires_at: "2026-08-05T12:00:00+00:00",
      csv_download: "https://evil.example/exfil",
      pdf_download: `/exports/downloads/${PDF_TOKEN}`,
    };
  }
  if (exportArtifactShape === "fractional_rows") {
    return {
      row_count: 3.5,
      truncated: false,
      row_limit: 10000,
      expires_at: "2026-08-05T12:00:00+00:00",
      csv_download: `/exports/downloads/${CSV_TOKEN}`,
      pdf_download: `/exports/downloads/${PDF_TOKEN}`,
    };
  }
  return {
    row_count: 3,
    truncated: true,
    row_limit: 10000,
    expires_at: "2026-08-05T12:00:00+00:00",
    csv_download: `/exports/downloads/${CSV_TOKEN}`,
    pdf_download: `/exports/downloads/${PDF_TOKEN}`,
  };
}

function exportOut(status: string, withArtifact: boolean): Record<string, unknown> {
  return {
    id: EXPORT_ID,
    report: "member_statement",
    status,
    filters: { member_id: MEMBER_ID },
    allowed_columns: ["member_no", "date", "txn_ref", "type"],
    // F-R4: a garbage timestamp on an audit surface must be REJECTED
    // at the boundary, never rendered as "Invalid Date".
    as_of: exportRecordShape === "garbage_timestamp" ? "yesterday-ish" : "2026-08-04T10:00:00+00:00",
    completed_at: withArtifact ? "2026-08-04T10:05:00+00:00" : null,
    created_at: "2026-08-04T10:00:00+00:00",
    artifact: withArtifact ? artifactOut() : null,
    // An internal server field beyond the contract — must be STRIPPED.
    internal_storage_key: "s3://bucket/secret-path",
  };
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
  const path = new URL(request.url).pathname;
  if (request.headers.get("authorization") === null) {
    return json(401, { category: "unauthenticated", correlation_id: "corr-a" });
  }
  if (path === "/exports" && request.method === "POST") {
    return json(201, exportOut("queued", false));
  }
  if (path === `/exports/${EXPORT_ID}` && request.method === "GET") {
    return json(200, exportOut("completed", true));
  }
  if (path === "/jobs/exports" && request.method === "POST") {
    return json(200, { completed: 2, failed: 0 });
  }
  if (path === `/exports/downloads/${CSV_TOKEN}` && request.method === "GET") {
    // The server's own rendered artifact (U2 on-screen view): CRLF
    // records, minimal quoting, truncation facts as HEADERS.
    return new Response('Member No,Amount\r\nGP-001,"1,234.56"\r\n', {
      status: 200,
      headers: {
        "content-type": "text/csv; charset=utf-8",
        "X-Export-Truncated": "true",
        "X-Export-Limit": "10000",
      },
    });
  }
  if (path === "/member-exits" && request.method === "GET") {
    return json(200, {
      items: [
        {
          id: EXIT_ID,
          member_id: MEMBER_ID,
          status: "requested",
          created_at: "2026-07-01T09:00:00+00:00",
          // Money fields of ExitOut — never consumed by this module.
          net_payable: "120000.10",
          shares_amount: "20000.00",
          deposits_amount: "110000.10",
          loan_balance: "10000.00",
          fees: "500.00",
          version: 1,
        },
      ],
      next_cursor: "exit-cursor-2",
    });
  }
  if (path === "/dividends/declarations" && request.method === "GET") {
    return json(200, {
      items: [
        {
          id: DECLARATION_ID,
          fy_start: "2025-01-01",
          fy_end: "2025-12-31",
          status: "declared",
          total_payout: "9999999.99",
          version: 2,
        },
      ],
      next_cursor: null,
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
const reportsApi = require("../api") as typeof import("../api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  exportArtifactShape = "ok";
  exportRecordShape = "ok";
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("POST /exports: body carries the report + PROVIDED filters ONLY; idempotency as a header; nothing in the query string; EXTRA response keys are STRIPPED", async () => {
  const result = await reportsApi.requestExport(
    { report: "member_statement", filters: { member_id: MEMBER_ID } },
    "key-exp-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe("/exports");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-exp-1");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.url).not.toContain(REFRESH_VALUE);

  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body).sort()).toEqual(["filters", "report"]);
  expect(body["report"]).toBe("member_statement");
  // ONLY the provided filter travels — empties are omitted, and no
  // format/limit/column/storage key exists anywhere in the body.
  expect(body["filters"]).toEqual({ member_id: MEMBER_ID });

  expect(result.id).toBe(EXPORT_ID);
  expect(result.status).toBe("queued");
  expect(result.artifact).toBeNull();
  // The stubbed internal storage field can never reach a screen.
  expect("internal_storage_key" in result).toBe(false);
});

test("POST /exports with NO filters: the filters key is ABSENT from the wire body (never an empty object of empty strings)", async () => {
  await reportsApi.requestExport({ report: "trial_balance", filters: {} }, "key-exp-2");
  const body = JSON.parse(calls[0]!.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body)).toEqual(["report"]);
  expect(body["report"]).toBe("trial_balance");
});

test("GET /exports/{id}: the id rides the PATH; the completed record parses with the server's artifact metadata verbatim", async () => {
  const result = await reportsApi.fetchExport(EXPORT_ID);
  const call = calls[0]!;
  expect(call.method).toBe("GET");
  expect(new URL(call.url).pathname).toBe(`/exports/${EXPORT_ID}`);
  expect(new URL(call.url).search).toBe("");

  expect(result.status).toBe("completed");
  expect(result.artifact).not.toBeNull();
  expect(result.artifact?.row_count).toBe(3);
  expect(result.artifact?.truncated).toBe(true);
  expect(result.artifact?.row_limit).toBe(10000);
  expect(result.artifact?.csv_download).toBe(`/exports/downloads/${CSV_TOKEN}`);
  // The capability token extracts from the contract path — and ONLY
  // from the export-download namespace.
  expect(reportsApi.downloadToken(result.artifact!.csv_download)).toBe(CSV_TOKEN);
  expect(() => reportsApi.downloadToken("https://evil.example/exfil")).toThrow();
});

test("a contract-violating download path (outside /exports/downloads/) is REJECTED at the boundary — it can never be followed", async () => {
  exportArtifactShape = "hostile_path";
  const thrown = await reportsApi.fetchExport(EXPORT_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("csv_download");
});

test("a fractional row count is a contract violation and is REJECTED, never silently rendered", async () => {
  exportArtifactShape = "fractional_rows";
  const thrown = await reportsApi.fetchExport(EXPORT_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("row_count");
});

test("F-R4: a garbage export timestamp is REJECTED at the boundary — 'Invalid Date' can never reach the operator-facing audit surface", async () => {
  exportRecordShape = "garbage_timestamp";
  const thrown = await reportsApi.fetchExport(EXPORT_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("as_of");
});

test("F-R4: the timestamp boundary accepts exactly the backend isoformat() shapes (offset, Z, naive, microseconds) and rejects everything else", () => {
  /* eslint-disable-next-line @typescript-eslint/no-require-imports */
  const schemas = require("../schemas") as typeof import("../schemas");
  const record = (as_of: string) => ({
    id: EXPORT_ID,
    report: "member_statement",
    status: "queued",
    filters: {},
    allowed_columns: ["member_no"],
    as_of,
    completed_at: null,
    created_at: "2026-08-04T10:00:00+00:00",
    artifact: null,
  });
  const accepted = [
    "2026-08-04T10:00:00+00:00", // aware isoformat (the live backend shape)
    "2026-08-04T10:00:00Z", // zulu suffix
    "2026-08-04T10:00:00", // naive isoformat
    "2026-08-04T10:00:00.123456+03:00", // microseconds + offset
  ];
  const rejected = [
    "yesterday-ish",
    "2026-08-04", // date-only: ExportOut timestamps are datetimes
    "04/08/2026 10:00",
    "",
  ];
  expect(
    accepted.filter((value) => !schemas.exportOutSchema.safeParse(record(value)).success),
  ).toEqual([]);
  expect(
    rejected.filter((value) => schemas.exportOutSchema.safeParse(record(value)).success),
  ).toEqual([]);
});

test("POST /jobs/exports: NO request body exists (the contract defines none); idempotency as a header; the cycle counts parse verbatim", async () => {
  const result = await reportsApi.runExportQueue("key-queue-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(new URL(call.url).pathname).toBe("/jobs/exports");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-queue-1");
  // openapi-fetch serialises an absent body as an empty payload — the
  // assertion is that NOTHING tunable travels.
  expect(call.body === null || call.body === "").toBe(true);
  expect(result.completed).toBe(2);
  expect(result.failed).toBe(0);
});

test("picker reads are keyset ONLY (cursor verbatim, no offset/page) and STRIP the records' settlement figures at the boundary", async () => {
  const exits = await reportsApi.fetchExitsPage(null);
  await reportsApi.fetchExitsPage("exit-cursor-2");
  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe("/member-exits");
  expect(first.searchParams.get("limit")).toBe(String(reportsApi.PICKER_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);
  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("exit-cursor-2");

  expect(exits.nextCursor).toBe("exit-cursor-2");
  const exitRow = exits.items[0]!;
  expect(exitRow.id).toBe(EXIT_ID);
  expect(exitRow.status).toBe("requested");
  // The picker row carries NO figures — everything money-shaped from
  // ExitOut was stripped (this module renders no figure at all).
  expect(Object.keys(exitRow).sort()).toEqual(["created_at", "id", "member_id", "status"]);

  const declarations = await reportsApi.fetchDeclarationsPage(null);
  const third = new URL(calls[2]!.url);
  expect(third.pathname).toBe("/dividends/declarations");
  expect(third.searchParams.has("offset")).toBe(false);
  const declarationRow = declarations.items[0]!;
  expect(declarationRow.id).toBe(DECLARATION_ID);
  expect(Object.keys(declarationRow).sort()).toEqual(["fy_end", "fy_start", "id", "status"]);
});

test("a requester-only 404 on another operator's export id surfaces as ONE least-disclosure ApiError — no retry", async () => {
  const otherId = "99999999-8888-7777-6666-555555555555";
  const thrown = await reportsApi.fetchExport(otherId).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(404);
  expect(calls).toHaveLength(1);
});

test("U2 on-screen view — GET /exports/downloads/{token} as TEXT: token rides the contract PATH via the generated client; auth/tenant are HEADERS; body and truncation headers return VERBATIM", async () => {
  const result = await reportsApi.fetchArtifactText(`/exports/downloads/${CSV_TOKEN}`);
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("GET");
  expect(new URL(call.url).pathname).toBe(`/exports/downloads/${CSV_TOKEN}`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.url).not.toContain(REFRESH_VALUE);
  // The artifact body byte-identical — including the quoted grouped
  // figure the strict parser must keep as ONE cell.
  expect(result.text).toBe('Member No,Amount\r\nGP-001,"1,234.56"\r\n');
  // Truncation facts VERBATIM from the response headers.
  expect(result.truncatedHeader).toBe("true");
  expect(result.limitHeader).toBe("10000");
});

test("U2: a path outside the export-download namespace is REFUSED before any request is issued", async () => {
  await expect(reportsApi.fetchArtifactText("https://evil.example/exfil")).rejects.toThrow();
  expect(calls).toHaveLength(0);
});

test("U2: an expired/unknown token surfaces ONE least-disclosure ApiError — the view never retries and never renders a partial artifact", async () => {
  const thrown = await reportsApi
    .fetchArtifactText("/exports/downloads/expired-token")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(404);
  expect(calls).toHaveLength(1);
});
