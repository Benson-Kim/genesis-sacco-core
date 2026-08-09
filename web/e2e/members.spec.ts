/**
 * Members E2E (P15 exit criterion backfill — issue #30 T1: batch-1
 * modules pre-dated the e2e harness and were never back-filled):
 * happy path INCLUDING the create flow proven at the browser wire
 * (exactly one POST, key-exact body, Idempotency-Key header) + the
 * forbidden-role adversarial flow, driven through the REAL production
 * build in a real browser.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin), extending the existing harness pattern (never rebuilt).
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "*",
};

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function jwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 900;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const FULL_PERMISSIONS = {
  role_id: "77777777-7777-7777-7777-777777777777",
  permissions: [
    { module: "members", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

const NO_MEMBERS_PERMISSIONS = {
  role_id: "66666666-6666-6666-6666-666666666666",
  permissions: [
    { module: "reports", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

const MEMBER_OUT = {
  id: MEMBER_ID,
  member_no: "GP-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: "+254700000001",
  email: "jane@sacco.co.ke",
  status: "active",
  version: 1,
  // #31 batch 7 (j).2: member reads carry the expand-only NULLABLE
  // branch attribution — the key is ALWAYS present (nullable-never-
  // optional), so the fixture must carry it for the Zod parse.
  branch_id: null,
  dividend_payout: null,
};

// #31 batch 3 review: the register opts in to the LIST aggregates.
// DELIBERATELY NON-ADDITIVE: no figure equals any combination of the
// others, so a register that summed or derived a figure would surface
// a string this spec asserts ABSENT.
const AGGREGATES_OUT = {
  deposits_total: "1000.11",
  shares_total: "200.22",
  loans_outstanding: "300.33",
  guarantees_pledged: "40.04",
};

interface ApiState {
  permissions: unknown;
  postBodies: Record<string, unknown>[];
  postHeaders: Record<string, string>[];
  listCalls: number;
  listIncludes: (string | null)[];
}

/** Browser-boundary API mock with CORS handling and write capture. */
async function mockApi(page: Page, state: ApiState): Promise<void> {
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    const respond = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: CORS_HEADERS,
        body: JSON.stringify(body),
      });

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }
    if (path === "/auth/otp/request" && method === "POST") {
      await respond(200, {});
      return;
    }
    if (path === "/auth/otp/verify" && method === "POST") {
      await respond(200, {
        access_token: jwt(ADMIN_ID),
        refresh_token: "e2e-refresh-1",
        expires_in: 900,
      });
      return;
    }
    if (path === "/auth/refresh" && method === "POST") {
      await respond(200, {
        access_token: jwt(ADMIN_ID),
        refresh_token: "e2e-refresh-rotated",
        expires_in: 900,
      });
      return;
    }
    if (path === "/me/permissions" && method === "GET") {
      await respond(200, state.permissions);
      return;
    }
    if (path === "/members" && method === "GET") {
      state.listCalls += 1;
      // Rows carry the aggregates object ONLY when the register opted
      // in with include=aggregates (#31 batch 3 review).
      const include = new URL(request.url()).searchParams.get("include");
      state.listIncludes.push(include);
      await respond(200, {
        items: [
          include === "aggregates" ? { ...MEMBER_OUT, aggregates: AGGREGATES_OUT } : MEMBER_OUT,
        ],
        next_cursor: null,
      });
      return;
    }
    if (path === "/members" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.postBodies.push(body);
      state.postHeaders.push(await request.allHeaders());
      await respond(201, {
        id: "22222222-3333-4444-5555-666666666666",
        member_no: "GP-0002",
        type: body["type"],
        name: body["name"],
        phone: body["phone"],
        email: body["email"],
        status: "active",
        version: 1,
        branch_id: null,
        dividend_payout: null,
      });
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

test("happy path: OTP login → member register renders → the create flow commits EXACTLY ONE key-exact POST with an Idempotency-Key header (wire proof)", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: FULL_PERMISSIONS,
    postBodies: [],
    postHeaders: [],
    listCalls: 0,
    listIncludes: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Members" }).click();
  // The register lists the server's records verbatim.
  await expect(page.getByText("Jane Wanjiku")).toBeVisible();
  await expect(page.getByText("GP-0001")).toBeVisible();

  // #31 batch 3 review: the register opted in (include=aggregates on
  // the wire) and renders the four SERVER aggregate strings VERBATIM —
  // and never a derived figure (non-additive fixture: deposits with
  // shares would read 1200.33; all four together 1540.70; deposits net
  // of loans 699.78).
  expect(state.listIncludes.length).toBeGreaterThanOrEqual(1);
  expect(state.listIncludes.every((value) => value === "aggregates")).toBe(true);
  await expect(page.getByText("1000.11", { exact: true })).toBeVisible();
  await expect(page.getByText("200.22", { exact: true })).toBeVisible();
  await expect(page.getByText("300.33", { exact: true })).toBeVisible();
  await expect(page.getByText("40.04", { exact: true })).toBeVisible();
  await expect(page.getByText("1200.33")).toHaveCount(0);
  await expect(page.getByText("1540.70")).toHaveCount(0);
  await expect(page.getByText("699.78")).toHaveCount(0);

  // The create flow, through the real drawer UI.
  await page.getByRole("button", { name: "Register member" }).click();
  const drawer = page.getByRole("dialog", { name: "New member registration" });
  await drawer.getByLabel("Full name").fill("Amina Otieno");
  await drawer.getByLabel("Phone (optional)").fill("+254711000002");
  await drawer.getByRole("button", { name: "Register member" }).click();

  // The SERVER's member number comes back and is announced verbatim.
  await expect(page.getByText("Member GP-0002 registered.")).toBeVisible();

  // Wire proof: exactly ONE POST; the body carries {type, name, phone,
  // email} and NOTHING else (member_no/status/accounts are server-owned);
  // the Idempotency-Key travels as a header next to bearer + tenant.
  expect(state.postBodies).toHaveLength(1);
  expect(state.postBodies[0]).toEqual({
    type: "person",
    name: "Amina Otieno",
    phone: "+254711000002",
    email: null,
  });
  expect(state.postHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.postHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(state.postHeaders[0]?.["x-tenant-id"]).toBeTruthy();

  // The register refetched after the create (list invalidation) — the
  // screen shows server truth, never a locally fabricated row.
  await expect
    .poll(() => state.listCalls, { message: "list refetch after create" })
    .toBeGreaterThanOrEqual(2);
});

test("adversarial forbidden-role: no members grant → no nav entry, deny-by-default route guard, NO register affordance", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: NO_MEMBERS_PERMISSIONS,
    postBodies: [],
    postHeaders: [],
    listCalls: 0,
    listIncludes: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: no Members entry mounts.
  await expect(page.getByRole("link", { name: "Reports" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Members", exact: true })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard: no register
  // renders, no write affordance exists, and the guarded screen never
  // even reads the list.
  await page.goto("/modules/members");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByRole("button", { name: "Register member" })).toHaveCount(0);
  expect(state.listCalls).toBe(0);
  expect(state.postBodies).toHaveLength(0);
});
