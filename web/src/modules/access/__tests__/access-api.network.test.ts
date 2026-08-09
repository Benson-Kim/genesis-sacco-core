/**
 * @jest-environment node
 *
 * Network-layer proofs for the access-control API layer through the
 * REAL generated client + middleware (node environment: real Request/
 * Response/Headers; fetch stubbed at the network boundary), mirroring
 * the exits/users reference harnesses (issue #30 findings T1/T3: the
 * permissions-matrix editor had no wire-level proof at all).
 * - Bearer/tenant/Idempotency-Key travel as HEADERS; nothing secret
 *   ever enters a URL (gate 1.6).
 * - role_id and module travel as PATH parameters serialized by the
 *   generated client — never as query strings or body fields.
 * - The PUT body is asserted KEY-BY-KEY: the four permission bits and
 *   NOTHING else — no role name, no module id, no actor can even be
 *   SENT (PermissionUpdateBody in api/access.py; the actor is the
 *   authenticated principal, server-side).
 * - ADVERSARIAL wire verdicts: a self-privilege-escalation attempt is
 *   refused by the SERVER with 403 and surfaces as ONE typed ApiError;
 *   a stale matrix write conflict (409) equally surfaces from ONE
 *   request — no transport-level retry, no replay.
 * - A contract-violating response (non-boolean permission bit) is
 *   REJECTED at the Zod boundary: a corrupted matrix can never arm the
 *   authz-sensitive UI; extra response keys are STRIPPED.
 */

// Module scope (the users reference harness stays a global script; two
// scripts would collide on shared global declarations under tsc).
export {};

type FetchCall = { url: string; method: string; headers: Headers; body: string | null };

const TENANT = "22222222-2222-2222-2222-222222222222";
const USER_ID = "55555555-5555-5555-5555-555555555555";
const ROLE_ID = "66666666-6666-6666-6666-666666666666";

const calls: FetchCall[] = [];
let putStatus = 200;
let listNonBooleanBit = false;

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string, expInSeconds: number): string {
  const exp = Math.floor(Date.now() / 1000) + expInSeconds;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const permissionOut = {
  module: "members",
  can_view: true,
  can_create: false,
  can_edit: false,
  can_approve: false,
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
  if (path === `/access/roles/${ROLE_ID}/permissions` && request.method === "GET") {
    if (listNonBooleanBit) {
      // A truthy STRING passes z.string()-era laxness and JS truthiness
      // checks — only the z.boolean() assertion stands between it and
      // the authz-sensitive matrix UI.
      return json(200, [{ ...permissionOut, can_edit: "yes" }]);
    }
    return json(200, [permissionOut]);
  }
  if (path.startsWith(`/access/roles/${ROLE_ID}/permissions/`) && request.method === "PUT") {
    if (putStatus !== 200) {
      return json(putStatus, {
        category: putStatus === 403 ? "forbidden" : "conflict",
        correlation_id: `corr-${putStatus}`,
      });
    }
    const requested = JSON.parse(body ?? "{}") as Record<string, unknown>;
    return json(200, {
      ...permissionOut,
      can_create: requested["can_create"] === true,
      internal_policy_note: "RBAC-SEED-SECRET",
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
const accessApi = require("../api") as typeof import("../api");
const { ApiError } = require("@genesis/api-client") as typeof import("@genesis/api-client");
/* eslint-enable @typescript-eslint/no-require-imports */

const REFRESH_VALUE = "per-tab-refresh-value";
const FULL_BITS = { can_view: true, can_create: true, can_edit: false, can_approve: false };

beforeEach(() => {
  calls.length = 0;
  putStatus = 200;
  listNonBooleanBit = false;
  session.clearSession();
  session.setSession({ accessToken: jwt(USER_ID, 900), refreshToken: REFRESH_VALUE });
});

afterEach(() => {
  session.clearSession();
});

test("GET role permissions: role_id travels as a PATH parameter with no query string; bearer + tenant are HEADERS; nothing secret in the URL", async () => {
  const rows = await accessApi.fetchRolePermissions(ROLE_ID);
  expect(calls).toHaveLength(1);
  const url = new URL(calls[0]!.url);
  expect(url.pathname).toBe(`/access/roles/${ROLE_ID}/permissions`);
  expect(url.search).toBe("");
  expect(calls[0]!.headers.get("authorization")).toMatch(/^Bearer /);
  expect(calls[0]!.headers.get("x-tenant-id")).toBe(TENANT);
  expect(calls[0]!.url).not.toContain(REFRESH_VALUE);
  expect(rows).toEqual([permissionOut]);
});

test("PUT permission: role_id AND module travel as PATH parameters; the body carries the four bits and NOTHING else; the Idempotency-Key is a HEADER; EXTRA response keys are STRIPPED", async () => {
  const updated = await accessApi.updateRolePermission(ROLE_ID, "members", FULL_BITS, "key-acl-1");
  expect(calls).toHaveLength(1);
  const call = calls[0]!;
  expect(call.method).toBe("PUT");
  const url = new URL(call.url);
  expect(url.pathname).toBe(`/access/roles/${ROLE_ID}/permissions/members`);
  expect(url.search).toBe("");
  expect(call.headers.get("idempotency-key")).toBe("key-acl-1");
  const body = JSON.parse(call.body ?? "{}") as Record<string, unknown>;
  expect(body["can_view"]).toBe(true);
  expect(body["can_create"]).toBe(true);
  expect(body["can_edit"]).toBe(false);
  expect(body["can_approve"]).toBe(false);
  // Nothing beyond PermissionUpdateBody can even be sent — the actor is
  // the authenticated principal and the target is the PATH (falsifiable:
  // add a key to the API layer and this fails).
  expect(Object.keys(body).sort()).toEqual(["can_approve", "can_create", "can_edit", "can_view"]);

  // The server row comes back verbatim; the stubbed internal field can
  // never reach the matrix UI (STRIPPED at the boundary).
  expect(updated.can_create).toBe(true);
  expect("internal_policy_note" in updated).toBe(false);
});

test("ADVERSARIAL self-privilege-escalation at the wire: the SERVER's 403 refusal surfaces as ONE typed ApiError — no transport-level retry", async () => {
  putStatus = 403;
  const thrown = await accessApi
    .updateRolePermission(ROLE_ID, "access_control", FULL_BITS, "key-esc")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(403);
  expect((thrown as InstanceType<typeof ApiError>).category).toBe("forbidden");
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});

test("ADVERSARIAL stale matrix write at the wire: a 409 conflict surfaces as ONE typed ApiError — no replay", async () => {
  putStatus = 409;
  const thrown = await accessApi
    .updateRolePermission(ROLE_ID, "members", FULL_BITS, "key-stale")
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(ApiError);
  expect((thrown as InstanceType<typeof ApiError>).status).toBe(409);
  expect((thrown as InstanceType<typeof ApiError>).category).toBe("conflict");
  expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
});

test("a contract-violating permission bit (truthy STRING) is REJECTED at the boundary — a corrupted matrix can never arm the authz-sensitive UI", async () => {
  listNonBooleanBit = true;
  const thrown = await accessApi
    .fetchRolePermissions(ROLE_ID)
    .catch((error: unknown) => error);
  expect(thrown).toBeInstanceOf(Error);
  expect(thrown).not.toBeInstanceOf(ApiError);
  expect(String(thrown)).toContain("can_edit");
});
