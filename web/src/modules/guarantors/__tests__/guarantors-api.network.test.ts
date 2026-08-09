/**
 * @jest-environment node
 *
 * Network-layer proofs for the guarantors API layer through the REAL
 * generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the loans/applications reference harnesses:
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL.
 * - MONEY travels as decimal STRINGS byte-identically in JSON bodies
 *   (blocker (a)) — never re-encoded as numbers.
 * - The release/consent bodies carry the VERSION only — no amounts,
 *   ever (P13.14: the released figure comes from the guarantee row).
 * - A blank substitution amount is OMITTED from the wire (the server
 *   derives the floor) — never sent as null/0.
 * - A stale versioned act surfaces as a typed 409 ApiError from ONE
 *   request (no transport-level retry); a contract-violating response
 *   (numeric money) is REJECTED at the Zod boundary, never returned.
 * - This module performs NO reads: the contract exposes no guarantee
 *   list/read endpoint and nothing here invents one (every request is
 *   a POST to the four write routes).
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const BORROWER_ID = "11111111-1111-1111-1111-111111111111";
const GUARANTOR_ID = "33333333-1111-2222-3333-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const GUARANTEE_ID = "bbbbbbbb-1111-2222-3333-444444444444";

const calls: FetchCall[] = [];
let releaseStatus = 200;
let guaranteeMoneyAsNumbers = false;
let guaranteeGarbageMoney = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function guaranteeOut(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: GUARANTEE_ID,
    application_id: APP_ID,
    loan_id: null,
    borrower_member_id: BORROWER_ID,
    guarantor_member_id: GUARANTOR_ID,
    amount: "50000.10",
    status: "pledged",
    version: 1,
    ...overrides,
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
  if (path === `/applications/${APP_ID}/guarantees` && request.method === "POST") {
    if (guaranteeMoneyAsNumbers) {
      return json(201, guaranteeOut({ amount: 50000.1 }));
    }
    if (guaranteeGarbageMoney) {
      // A STRING, so it passes a naive z.string() — the shared
      // moneySchema shape (issue #30 A2/S2) is the only thing standing
      // between it and fmtKes in the act dialogs.
      return json(201, guaranteeOut({ amount: "007.10" }));
    }
    return json(201, guaranteeOut({ internal_capacity_note: "CAP-SECRET-01" }));
  }
  if (path === `/guarantees/${GUARANTEE_ID}/consent` && request.method === "POST") {
    return json(200, guaranteeOut({ status: "active", version: 2 }));
  }
  if (path === `/guarantees/${GUARANTEE_ID}/release` && request.method === "POST") {
    if (releaseStatus !== 200) {
      return json(releaseStatus, { category: "conflict", correlation_id: "corr-c" });
    }
    return json(200, guaranteeOut({ status: "released", version: 3 }));
  }
  if (path === `/guarantees/${GUARANTEE_ID}/substitute` && request.method === "POST") {
    return json(201, {
      released: guaranteeOut({ status: "released", version: 2 }),
      replacement: guaranteeOut({
        id: "eeeeeeee-1111-2222-3333-444444444444",
        status: "active",
        version: 1,
      }),
      internal_lock_trace: "LOCK-SECRET-01",
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
const guarantorsApi = require("../api") as typeof import("../api");
const guarantorSchemas = require("../schemas") as typeof import("../schemas");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";

beforeEach(() => {
  calls.length = 0;
  releaseStatus = 200;
  guaranteeMoneyAsNumbers = false;
  guaranteeGarbageMoney = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("POST /applications/{id}/guarantees: the amount travels as a decimal STRING byte-identically; ids ride the PATH; idempotency as a header; extra response keys are STRIPPED", async () => {
  const result = await guarantorsApi.pledgeGuarantee(
    APP_ID,
    { guarantor_member_id: GUARANTOR_ID, amount: "50000.10" },
    "key-pledge-1",
  );
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("POST");
  expect(new URL(call.url).pathname).toBe(`/applications/${APP_ID}/guarantees`);
  expect(new URL(call.url).search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-pledge-1");
  // String on the wire — falsifiable: coerce to Number and the trailing
  // ".10" collapses / the JSON type flips. Assert the raw bytes too.
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["amount"]).toBe("50000.10");
  expect(typeof body["amount"]).toBe("string");
  expect(call.body).toContain('"amount":"50000.10"');
  expect(Object.keys(body).sort()).toEqual(["amount", "guarantor_member_id"]);

  // Bearer + tenant as headers; the refresh secret never enters a URL.
  expect(call.headers.get("authorization")).toMatch(/^Bearer /);
  expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  expect(call.url).not.toContain(REFRESH_VALUE);

  // The Zod boundary strips anything beyond the contract — the stubbed
  // internal capacity note can never reach a screen.
  expect(result.id).toBe(GUARANTEE_ID);
  expect(result.amount).toBe("50000.10");
  expect("internal_capacity_note" in result).toBe(false);
});

test("POST consent + release: no amounts EVER (P13.14); consent carries version + the MANDATORY evidence citation (P14.5 staff-attested override), release the version only; the server's new status/version parse verbatim", async () => {
  const consented = await guarantorsApi.consentGuarantee(
    GUARANTEE_ID,
    1,
    "Signed consent form CF-14",
    "key-consent-1",
  );
  const released = await guarantorsApi.releaseGuarantee(GUARANTEE_ID, 2, "key-release-1");
  expect(calls).toHaveLength(2);

  expect(new URL(calls[0]!.url).pathname).toBe(`/guarantees/${GUARANTEE_ID}/consent`);
  expect(JSON.parse(calls[0]!.body ?? "{}")).toEqual({
    version: 1,
    consent_reference: "Signed consent form CF-14",
  });
  expect(calls[0]!.headers.get("idempotency-key")).toBe("key-consent-1");

  expect(new URL(calls[1]!.url).pathname).toBe(`/guarantees/${GUARANTEE_ID}/release`);
  expect(JSON.parse(calls[1]!.body ?? "{}")).toEqual({ version: 2 });
  expect(calls[1]!.headers.get("idempotency-key")).toBe("key-release-1");

  expect(consented.status).toBe("active");
  expect(consented.version).toBe(2);
  expect(released.status).toBe("released");
  expect(released.version).toBe(3);
});

test("a stale release surfaces as ONE 409 ApiError — no transport-level retry, no replay", async () => {
  releaseStatus = 409;
  const thrown = await guarantorsApi
    .releaseGuarantee(GUARANTEE_ID, 1, "key-release-stale")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

test("POST substitute: a BLANK replacement amount is OMITTED from the wire (server-derived floor); the evidence citation travels verbatim and NO consented boolean exists anywhere (P14.5 — a caller-asserted consent flag is a rejected design); the swap boundary STRIPS extras", async () => {
  const swap = await guarantorsApi.substituteGuarantee(
    GUARANTEE_ID,
    2,
    {
      guarantor_member_id: GUARANTOR_ID,
      consent_reference: "Signed guarantorship form GF-778",
      amount: null,
    },
    "key-substitute-1",
  );
  expect(calls).toHaveLength(1);
  const body = JSON.parse(calls[0]!.body ?? "{}") as Record<string, unknown>;
  // No amount key AT ALL — the server derives the floor from the row —
  // and no consented boolean (the !29 lesson made structural).
  expect(Object.keys(body).sort()).toEqual([
    "consent_reference",
    "guarantor_member_id",
    "version",
  ]);
  expect("consented" in body).toBe(false);
  expect(body["consent_reference"]).toBe("Signed guarantorship form GF-778");
  expect(body["version"]).toBe(2);

  // Both swap sides parse; the internal lock trace is STRIPPED.
  expect(swap.released.status).toBe("released");
  expect(swap.replacement.status).toBe("active");
  expect("internal_lock_trace" in swap).toBe(false);
});

test("a typed replacement amount travels as the decimal STRING byte-identically", async () => {
  await guarantorsApi.substituteGuarantee(
    GUARANTEE_ID,
    2,
    {
      guarantor_member_id: GUARANTOR_ID,
      consent_reference: "Signed guarantorship form GF-779",
      amount: "60000.10",
    },
    "key-substitute-2",
  );
  expect(calls).toHaveLength(1);
  expect(calls[0]!.body).toContain('"amount":"60000.10"');
});

test("a contract-violating response (numeric money) is REJECTED at the boundary — it can never reach a screen", async () => {
  guaranteeMoneyAsNumbers = true;
  const thrown = await guarantorsApi
    .pledgeGuarantee(APP_ID, { guarantor_member_id: GUARANTOR_ID, amount: "50000.10" }, "key-x")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("amount");
});

test("a garbage money STRING is REJECTED at the wire boundary (issue #30 A2/S2) — a value that passes z.string() can still never reach fmtKes", async () => {
  guaranteeGarbageMoney = true;
  const thrown = await guarantorsApi
    .pledgeGuarantee(APP_ID, { guarantor_member_id: GUARANTOR_ID, amount: "50000.10" }, "key-g")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("amount");
});

test("issue #30 A2/S2 accept/reject matrix: guarantees.amount asserts the CANONICAL server shape (numeric(18,2) CHECK > 0 — migration 0001)", () => {
  const withAmount = (value: string) =>
    guarantorSchemas.guaranteeSchema.safeParse(guaranteeOut({ amount: value })).success;

  // Canonical column shapes ACCEPTED (str(Decimal), hand-computed).
  expect(withAmount("50000.10")).toBe(true);
  expect(withAmount("0.01")).toBe(true);

  // Garbage shapes REJECTED — each previously flowed into fmtKes
  // unchallenged (bare z.string()).
  for (const value of ["abc", "1e5", "007.10", "50000.1", "50000.100", "50000", "50,000.10", " 50000.10", "NaN", ""]) {
    expect(withAmount(value)).toBe(false);
  }
  // CHECK (amount > 0): a '-' is a contract violation.
  expect(withAmount("-1.00")).toBe(false);
});

test("this module performs NO reads and NO query strings: every request is a POST to the four write routes (no offset/page/list surface exists)", async () => {
  await guarantorsApi.pledgeGuarantee(
    APP_ID,
    { guarantor_member_id: GUARANTOR_ID, amount: "50000.10" },
    "key-pledge-2",
  );
  await guarantorsApi.consentGuarantee(
    GUARANTEE_ID,
    1,
    "Signed consent form CF-15",
    "key-consent-2",
  );
  await guarantorsApi.releaseGuarantee(GUARANTEE_ID, 2, "key-release-2");
  for (const call of calls) {
    expect(call.method).toBe("POST");
    const url = new URL(call.url);
    expect(url.search).toBe("");
    expect(url.searchParams.has("offset")).toBe(false);
    expect(url.searchParams.has("page")).toBe(false);
    expect(call.headers.get("authorization")).toMatch(/^Bearer /);
    expect(call.headers.get("x-tenant-id")).toBe(TENANT);
  }
});
