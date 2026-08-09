/**
 * Batch-7 read models E2E (#31 ledger (f)/(g)/(j)): the legs
 * drill-down, the branch roster panels and the dormancy worklist +
 * operations run, driven through the REAL production build in a real
 * browser (OTP login UI, session custody, deny-by-default guards,
 * generated client, typed confirmation).
 *
 * The API is mocked at the BROWSER network boundary (page.route):
 * request counts/bodies/headers are asserted server-side-of-the-wire,
 * so the structural-withholding proofs (no grant → ZERO roster/legs
 * requests) and the single-write dormancy proof measure real network
 * effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const TXN_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";

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
    { module: "transactions", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: true, can_approve: false },
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "access_control", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** FULL settings rights but NO people-module grant at all — the
 * structural-withholding role for the roster split. */
const SETTINGS_ONLY_PERMISSIONS = {
  role_id: "66666666-6666-6666-6666-666666666666",
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: false },
  ],
};

/** members:view ONLY — the dormancy forbidden-role leg. */
const MEMBERS_VIEW_ONLY_PERMISSIONS = {
  role_id: "55555555-6666-7777-8888-999999999999",
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

const TXN_OUT = {
  id: TXN_ID,
  txn_ref: "RP-0100",
  member_id: null,
  type: "loan_repayment",
  amount: "100.00",
  channel: "mpesa",
  direction: "credit",
  occurred_at: "2026-07-18T10:15:00+00:00",
  is_reversal: false,
  created_by: null,
  external_ref: null,
};

/** NON-ADDITIVE legs fixture: gross 200.00 / net 0.00 must NEVER
 * render — the no-client-side-balancing proof. */
const LEGS_OUT = {
  items: [
    { account: "cash.mpesa", side: "debit", amount: "100.00" },
    { account: "income.interest", side: "credit", amount: "40.00" },
    { account: "loans.receivable", side: "credit", amount: "60.00" },
  ],
};

const BRANCH_OUT = {
  id: BRANCH_ID,
  name: "Nairobi West",
  version: 1,
  created_at: "2026-07-01T08:00:00+00:00",
};

const USER_ROSTER_OUT = {
  items: [
    {
      id: "aaaaaaaa-5555-6666-7777-888888888888",
      full_name: "Ann Achieng",
      email: "ann@sacco.example",
      status: "active",
    },
  ],
  next_cursor: null,
};

const MEMBER_ROSTER_OUT = {
  items: [
    {
      id: "cccccccc-5555-6666-7777-888888888888",
      member_no: "GP-7001",
      name: "Alpha Member",
      status: "dormant",
    },
  ],
  next_cursor: null,
};

const DORMANT_MEMBER_OUT = {
  id: MEMBER_ID,
  member_no: "GP-0031",
  type: "person",
  name: "Grace Njeri",
  phone: "+254700000031",
  email: null,
  status: "dormant",
  version: 4,
  branch_id: null,
  dividend_payout: null,
};

const DORMANCY_RUN_OUT = {
  as_of: "2026-08-06",
  cutoff: "2025-08-06",
  period_months: 12,
  scanned: 240,
  transitioned: 7,
  batches: 2,
};

interface ApiState {
  permissions?: unknown;
  legsGets: number;
  userRosterGets: number;
  memberRosterGets: number;
  dormancyPostBodies: Record<string, unknown>[];
  dormancyPostHeaders: Record<string, string>[];
}

function newState(permissions?: unknown): ApiState {
  return {
    permissions,
    legsGets: 0,
    userRosterGets: 0,
    memberRosterGets: 0,
    dormancyPostBodies: [],
    dormancyPostHeaders: [],
  };
}

async function mockApi(page: Page, state: ApiState): Promise<void> {
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
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
      await respond(200, state.permissions ?? FULL_PERMISSIONS);
      return;
    }
    if (path === "/transactions" && method === "GET") {
      await respond(200, { items: [TXN_OUT], next_cursor: null });
      return;
    }
    if (path === `/transactions/${TXN_ID}/legs` && method === "GET") {
      state.legsGets += 1;
      await respond(200, LEGS_OUT);
      return;
    }
    if (path === "/branches" && method === "GET") {
      await respond(200, { items: [BRANCH_OUT], next_cursor: null });
      return;
    }
    if (path === `/branches/${BRANCH_ID}` && method === "GET") {
      await respond(200, BRANCH_OUT);
      return;
    }
    if (path === `/branches/${BRANCH_ID}/users` && method === "GET") {
      state.userRosterGets += 1;
      await respond(200, USER_ROSTER_OUT);
      return;
    }
    if (path === `/branches/${BRANCH_ID}/members` && method === "GET") {
      state.memberRosterGets += 1;
      await respond(200, MEMBER_ROSTER_OUT);
      return;
    }
    if (path === "/members" && method === "GET") {
      // The dormancy worklist pins status=dormant server-side.
      if (url.searchParams.get("status") === "dormant") {
        await respond(200, { items: [DORMANT_MEMBER_OUT], next_cursor: null });
        return;
      }
      await respond(200, { items: [], next_cursor: null });
      return;
    }
    if (path === "/members/jobs/dormancy" && method === "POST") {
      state.dormancyPostBodies.push(request.postDataJSON() as Record<string, unknown>);
      state.dormancyPostHeaders.push(await request.allHeaders());
      await respond(200, DORMANCY_RUN_OUT);
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("operator@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

test("legs drill-down (g): the drawer renders every DR/CR leg VERBATIM from ONE wire read — and no client-side balancing total exists (non-additive fixture)", async ({
  page,
}) => {
  const state = newState();
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Transactions" }).click();
  await page.getByText("RP-0100").click();

  const drawer = page.getByRole("dialog", { name: "Transaction detail" });
  await expect(drawer.getByText("Double-entry legs")).toBeVisible();
  await expect(drawer.getByText("cash.mpesa")).toBeVisible();
  await expect(drawer.getByText("income.interest")).toBeVisible();
  await expect(drawer.getByText("loans.receivable")).toBeVisible();
  await expect(drawer.getByText("KES 40.00")).toBeVisible();
  await expect(drawer.getByText("KES 60.00")).toBeVisible();
  // NON-ADDITIVE proof (review F8: scoped to the drawer under test —
  // a page-wide probe could pass or fail on unrelated register
  // content): neither the gross (200.00) nor the net (0.00) of the
  // legs renders in the drawer, and nothing in it claims a total.
  await expect(drawer.getByText("KES 200.00")).toHaveCount(0);
  await expect(drawer.getByText("KES 0.00")).toHaveCount(0);
  await expect(drawer.getByText(/Total/)).toHaveCount(0);
  // Exactly ONE wire read served the section.
  expect(state.legsGets).toBe(1);
});

test("branch rosters (j).1 happy: both panels render their SERVER rows under the view grants", async ({
  page,
}) => {
  const state = newState();
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Branches" }).click();
  await page.getByText("Nairobi West").first().click();

  const drawer = page.getByRole("dialog", { name: "Branch" });
  await expect(drawer.getByText("Users assigned to this branch")).toBeVisible();
  await expect(drawer.getByText("Ann Achieng")).toBeVisible();
  await expect(drawer.getByText("Members assigned to this branch")).toBeVisible();
  await expect(drawer.getByText("Alpha Member")).toBeVisible();
  // The dormant status renders honestly, verbatim.
  await expect(drawer.getByText("Dormant")).toBeVisible();
  expect(state.userRosterGets).toBe(1);
  expect(state.memberRosterGets).toBe(1);
});

test("branch rosters (j).1 forbidden role: FULL settings rights alone mount NEITHER roster panel and ZERO roster requests reach the wire (structural withholding)", async ({
  page,
}) => {
  const state = newState(SETTINGS_ONLY_PERMISSIONS);
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Branches" }).click();
  await page.getByText("Nairobi West").first().click();

  const drawer = page.getByRole("dialog", { name: "Branch" });
  // The drawer itself serves the settings operator (rename mounts)…
  await expect(drawer.getByRole("button", { name: "Rename branch" })).toBeVisible();
  // …but no roster surface exists and NOTHING was probed at the wire.
  await expect(drawer.getByText("Users assigned to this branch")).toHaveCount(0);
  await expect(drawer.getByText("Members assigned to this branch")).toHaveCount(0);
  expect(state.userRosterGets).toBe(0);
  expect(state.memberRosterGets).toBe(0);
});

test("dormancy worklist (f) happy: the pinned dormant register renders; the typed confirmation commits exactly ONE POST with the server's documented batch size; counts render VERBATIM", async ({
  page,
}) => {
  const state = newState();
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Dormancy" }).click();
  await expect(page.getByText("Grace Njeri")).toBeVisible();
  await expect(page.getByText("GP-0031")).toBeVisible();

  await page.getByRole("button", { name: "Run dormancy scan…" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Run dormancy scan" });
  await dialog.getByRole("button", { name: "Run dormancy scan…" }).click();
  const confirm = page.getByRole("dialog", { name: "Confirm dormancy scan" });
  const confirmButton = confirm.getByRole("button", { name: "Run dormancy scan" });
  // The typed confirmation arms the write; nothing fired yet.
  await expect(confirmButton).toBeDisabled();
  expect(state.dormancyPostBodies).toHaveLength(0);
  await confirm.getByLabel('Type "mark dormant" to confirm').fill("mark dormant");
  await confirmButton.click();

  // The SERVER's verbatim report replaces the action…
  await expect(page.getByText("Dormancy run report")).toBeVisible();
  await expect(page.getByText("2026-08-06")).toBeVisible();
  await expect(page.getByText("240")).toBeVisible();
  // …and exactly ONE write reached the wire with the documented
  // batch-size default ONLY (no as_of, no period can even travel).
  expect(state.dormancyPostBodies).toHaveLength(1);
  expect(state.dormancyPostBodies[0]).toEqual({ batch_size: 200 });
  expect(state.dormancyPostHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.dormancyPostHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("dormancy (f) forbidden role: members:view alone gets the worklist but NO run affordance; zero job POSTs reach the wire", async ({
  page,
}) => {
  const state = newState(MEMBERS_VIEW_ONLY_PERMISSIONS);
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Dormancy" }).click();
  await expect(page.getByText("Grace Njeri")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run dormancy scan…" })).toHaveCount(0);
  expect(state.dormancyPostBodies).toHaveLength(0);
});
