/**
 * @jest-environment node
 *
 * Wire-level proofs for the member-KYC API layer (issue #31 batch 3)
 * through the REAL generated client + middleware (node environment:
 * real Request/Response/Headers; fetch stubbed at the network
 * boundary), mirroring the members/exits reference harnesses.
 *
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; KYC values are
 *   PII and NOTHING ever enters a URL beyond the member/document ids
 *   as path parameters (gate 1.6).
 * - Keyset pagination ONLY on the documents read: opaque cursor echoed
 *   back VERBATIM; no offset/page parameter exists (gate 1.3).
 * - Write bodies are asserted KEY-BY-KEY (nothing beyond the contract
 *   can even be sent); the document update proves the EXCLUDE-UNSET
 *   contract (review K2): a status-only body carries NO expires_at
 *   key, an expiry clear carries the EXPLICIT null.
 * - The 422 leg is MANDATORY (batch-1 close-out rule): FastAPI detail
 *   parses to ONE typed ApiError with canonical field keys.
 * - A 409 surfaces from ONE request — no transport retry, no replay.
 * - Boundary accept/reject with hand-computed oracles: a wrong-type
 *   profile payload, a malformed informational amount and an unknown
 *   document status are contract violations and are REJECTED.
 */

export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PROFILE_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const DOC_ID = "bbbbbbbb-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let profilePostStatus = 201;
let profileMalformedIncome = false;
let documentUnknownStatus = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const personProfilePayload = {
  bio: {
    first_name: "Jane",
    surname: "Wanjiku",
    gender: "Female",
    date_of_birth: "1990-01-01",
    id_number: "12345678",
    kra_pin: "A012345678Z",
  },
  contact: {
    phone: "+254700000001",
    email: null,
    county: "Nairobi",
    physical_address: "Moi Avenue 10",
  },
  employment: {
    employment_status: "Employed",
    occupation: "Teacher",
    monthly_income: "35000.50",
  },
  next_of_kin: { name: "Peter Wanjiku", relationship: "Brother", phone: "+254700000002" },
};

const profileOut = {
  id: PROFILE_ID,
  member_id: MEMBER_ID,
  member_type: "person",
  category: "Ordinary",
  profile: personProfilePayload,
  dpa_consent_at: "2026-08-01T09:00:00+00:00",
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

const documentOut = {
  id: DOC_ID,
  member_id: MEMBER_ID,
  doc_type: "national_id_copy",
  status: "pending",
  expires_at: null,
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
  if (path === `/members/${MEMBER_ID}/profile` && request.method === "GET") {
    if (profileMalformedIncome) {
      return json(200, {
        ...profileOut,
        profile: {
          ...personProfilePayload,
          employment: { ...personProfilePayload.employment, monthly_income: "1e5" },
        },
      });
    }
    return json(200, { ...profileOut, internal_scoring_seed: "SECRET" });
  }
  if (path === `/members/${MEMBER_ID}/profile` && request.method === "POST") {
    if (profilePostStatus === 409) {
      return json(409, { category: "conflict", correlation_id: "corr-c" });
    }
    if (profilePostStatus === 422) {
      // FastAPI validation body — the loc head is stripped to the
      // CANONICAL field key by toApiError (W56-5).
      return json(422, {
        detail: [{ loc: ["body", "dpa_consent"], msg: "Field required" }],
      });
    }
    return json(201, profileOut);
  }
  if (path === `/members/${MEMBER_ID}/profile` && request.method === "PUT") {
    return json(200, { ...profileOut, version: 2 });
  }
  if (path === `/members/${MEMBER_ID}/documents` && request.method === "GET") {
    if (documentUnknownStatus) {
      return json(200, { items: [{ ...documentOut, status: "approved" }], next_cursor: null });
    }
    return json(200, { items: [documentOut], next_cursor: "cursor-docs-2" });
  }
  if (path === `/members/${MEMBER_ID}/documents` && request.method === "POST") {
    return json(201, documentOut);
  }
  if (path === `/members/${MEMBER_ID}/documents/${DOC_ID}` && request.method === "PUT") {
    return json(200, { ...documentOut, status: "received", version: 2 });
  }
  return json(404, { category: "not_found", correlation_id: "corr-n" });
}

// The generated client captures globalThis.fetch at creation time, so the
// stub must be installed BEFORE the modules under test are imported.
globalThis.fetch = fetchStub as typeof globalThis.fetch;

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
const kycApi = require("../kycApi") as typeof import("../kycApi");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  profilePostStatus = 201;
  profileMalformedIncome = false;
  documentUnknownStatus = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET profile: member id as a PATH parameter, EMPTY query string, headers-only secrets; EXTRA response keys are STRIPPED", async () => {
  const profile = await kycApi.fetchKycProfile(MEMBER_ID);
  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe(`/members/${MEMBER_ID}/profile`);
  expect(url.search).toBe("");
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);
  // The stubbed internal field can never reach a screen.
  expect("internal_scoring_seed" in profile).toBe(false);
  expect(profile.version).toBe(1);
  // The nested informational figure arrives as the verbatim STRING.
  expect(
    (profile.profile as Record<string, Record<string, unknown>>)["employment"]![
      "monthly_income"
    ],
  ).toBe("35000.50");
});

test("POST profile: body carries {category, profile, dpa_consent} and NOTHING else; Idempotency-Key as a HEADER; EMPTY write query string", async () => {
  await kycApi.createKycProfile(
    MEMBER_ID,
    { category: "Ordinary", profile: personProfilePayload, dpa_consent: true },
    "key-profile-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe(`/members/${MEMBER_ID}/profile`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-profile-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  // KEY-EXACT: the member type is NEVER caller-supplied (the server
  // resolves it from the member row) — falsifiable: add a key to the
  // API layer and this fails.
  expect(Object.keys(body).sort()).toEqual(["category", "dpa_consent", "profile"]);
  expect(body["dpa_consent"]).toBe(true);
  const wireProfile = body["profile"] as Record<string, Record<string, unknown>>;
  expect(wireProfile["employment"]!["monthly_income"]).toBe("35000.50");
  expect(wireProfile["contact"]!["email"]).toBeNull();
});

test("PUT profile: version-pinned body, key-exact; the SAME key retries identically and a caller-rotated key travels verbatim", async () => {
  await kycApi.updateKycProfile(
    MEMBER_ID,
    { version: 1, category: "Ordinary", profile: personProfilePayload },
    "key-update-1",
  );
  await kycApi.updateKycProfile(
    MEMBER_ID,
    { version: 1, category: "Ordinary", profile: personProfilePayload },
    "key-update-1",
  );
  await kycApi.updateKycProfile(
    MEMBER_ID,
    { version: 2, category: "Premium", profile: personProfilePayload, dpa_consent: true },
    "key-update-2",
  );
  const puts = calls.filter((call) => call.method === "PUT");
  expect(puts).toHaveLength(3);
  expect(puts[0]!.headers.get("idempotency-key")).toBe("key-update-1");
  expect(puts[1]!.headers.get("idempotency-key")).toBe("key-update-1");
  expect(puts[2]!.headers.get("idempotency-key")).toBe("key-update-2");

  const first = JSON.parse(puts[0]!.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(first).sort()).toEqual(["category", "profile", "version"]);
  expect(first["version"]).toBe(1);
  // Consent travels ONLY when this submission grants it — and only as
  // the literal true (the contract accepts no other value).
  const third = JSON.parse(puts[2]!.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(third).sort()).toEqual(["category", "dpa_consent", "profile", "version"]);
  expect(third["dpa_consent"]).toBe(true);
});

test("GET documents: keyset params ONLY (no offset/page); the opaque cursor echoes back VERBATIM", async () => {
  await kycApi.fetchKycDocumentsPage(MEMBER_ID, null);
  const page = await kycApi.fetchKycDocumentsPage(MEMBER_ID, "cursor-docs-2");
  expect(calls).toHaveLength(2);

  const first = new URL(calls[0]!.url);
  expect(first.pathname).toBe(`/members/${MEMBER_ID}/documents`);
  expect(first.searchParams.get("limit")).toBe(String(kycApi.KYC_DOCUMENTS_PAGE_SIZE));
  expect(first.searchParams.has("cursor")).toBe(false);
  expect(first.searchParams.has("offset")).toBe(false);
  expect(first.searchParams.has("page")).toBe(false);

  const second = new URL(calls[1]!.url);
  expect(second.searchParams.get("cursor")).toBe("cursor-docs-2");
  expect(second.searchParams.has("offset")).toBe(false);

  expect(page.items[0]?.doc_type).toBe("national_id_copy");
  expect(page.nextCursor).toBe("cursor-docs-2");
});

test("POST document: body carries {doc_type} ONLY when no expiry is set (key-exact)", async () => {
  await kycApi.createKycDocument(MEMBER_ID, { doc_type: "national_id_copy" }, "key-doc-1");
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-doc-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(body)).toEqual(["doc_type"]);
  expect(body["doc_type"]).toBe("national_id_copy");
});

test("PUT document: EXCLUDE-UNSET honoured at the wire (review K2) — a status-only body carries NO expires_at key; an expiry clear carries the EXPLICIT null", async () => {
  await kycApi.updateKycDocument(MEMBER_ID, DOC_ID, { version: 1, status: "received" }, "key-a");
  await kycApi.updateKycDocument(MEMBER_ID, DOC_ID, { version: 2, expires_at: null }, "key-b");
  await kycApi.updateKycDocument(
    MEMBER_ID,
    DOC_ID,
    { version: 3, expires_at: "2027-01-01" },
    "key-c",
  );
  const puts = calls.filter((call) => call.method === "PUT");
  expect(puts).toHaveLength(3);

  const statusOnly = JSON.parse(puts[0]!.body ?? "{}") as Record<string, unknown>;
  // A missing key means "keep the stored expiry" — sending null here
  // would CLEAR a real date (the K2 trap this proves closed).
  expect(Object.keys(statusOnly).sort()).toEqual(["status", "version"]);
  expect(statusOnly["status"]).toBe("received");

  const clearExpiry = JSON.parse(puts[1]!.body ?? "{}") as Record<string, unknown>;
  expect(Object.keys(clearExpiry).sort()).toEqual(["expires_at", "version"]);
  expect(clearExpiry["expires_at"]).toBeNull();

  const setExpiry = JSON.parse(puts[2]!.body ?? "{}") as Record<string, unknown>;
  expect(setExpiry["expires_at"]).toBe("2027-01-01");
  expect(new URL(puts[0]!.url).pathname).toBe(`/members/${MEMBER_ID}/documents/${DOC_ID}`);
});

test("a 422 surfaces field-level messages as ONE typed ApiError (canonical field keys, W56-5) — never a silent success", async () => {
  profilePostStatus = 422;
  const thrown = await kycApi
    .createKycProfile(
      MEMBER_ID,
      { category: "Ordinary", profile: personProfilePayload, dpa_consent: true },
      "key-422",
    )
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  const apiError = thrown as InstanceType<typeof ApiError>;
  expect(apiError.status).toBe(422);
  expect(apiError.category).toBe("validation_error");
  expect(apiError.fields).toEqual([{ field: "dpa_consent", message: "Field required" }]);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("a 409 (duplicate profile / key reuse with a different hash) surfaces as ONE 409 ApiError — no transport retry, no replay", async () => {
  profilePostStatus = 409;
  const thrown = await kycApi
    .createKycProfile(
      MEMBER_ID,
      { category: "Ordinary", profile: personProfilePayload, dpa_consent: false },
      "key-conflict",
    )
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect((thrown as InstanceType<typeof ApiError>).category).toBe("conflict");
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("boundary REJECT: a malformed informational amount in the response is a contract violation (hand-computed oracle: '1e5' fails the backend _AMOUNT_PATTERN mirror)", async () => {
  profileMalformedIncome = true;
  const thrown = await kycApi.fetchKycProfile(MEMBER_ID).catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("profile");
});

test("boundary REJECT: an unknown document status never renders", async () => {
  documentUnknownStatus = true;
  const thrown = await kycApi
    .fetchKycDocumentsPage(MEMBER_ID, null)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("status");
});
