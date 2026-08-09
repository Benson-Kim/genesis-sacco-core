/**
 * Guarantors E2E (P15 exit criterion: per-module happy path + adversarial
 * flows), driven through the REAL production build in a real browser —
 * OTP login UI, session custody, deny-by-default guards, generated
 * client, typed-confirmation pledge.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies are asserted server-side-of-the-
 * wire, so the single-write double-pledge proof measures real network
 * effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const BORROWER_ID = "11111111-1111-1111-1111-111111111111";
const GUARANTOR_ID = "22222222-1111-2222-3333-444444444444";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const GUARANTEE_ID = "bbbbbbbb-1111-2222-3333-444444444444";

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
    { module: "applications", can_view: true, can_create: true, can_edit: true, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function applicationOut(overrides: Record<string, unknown> = {}) {
  return {
    id: APP_ID,
    member_id: BORROWER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
    rate_pct: "12.50",
    purpose: "School fees",
    stage: "submitted",
    cover_pct: "80.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 2,
    ...overrides,
  };
}

const SUMMARY_OUT = {
  as_of: "2026-08-04T06:00:00Z",
  guarantors: {
    active_guarantees: 7,
    total_pledged: "1234567.10",
    distinct_guarantors: 4,
    guarantors: [
      {
        member_id: GUARANTOR_ID,
        member_no: "M-0002",
        name: "Peter Otieno",
        pledged_total: "500000.10",
        free_capacity: "250000.10",
      },
    ],
  },
};

const PRODUCT_OUT = {
  id: PRODUCT_ID,
  name: "Development Loan",
  rate_pct: "12.50",
  deposit_multiplier: "3.00",
  max_term_months: 48,
  guarantors_required: 2,
  active: true,
  version: 1,
};

const BORROWER_OUT = {
  id: BORROWER_ID,
  member_no: "M-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active",
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

const GUARANTOR_OUT = {
  id: GUARANTOR_ID,
  member_no: "M-0002",
  type: "person",
  name: "Peter Otieno",
  phone: null,
  email: null,
  status: "active",
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

const GUARANTEE_OUT = {
  id: GUARANTEE_ID,
  application_id: APP_ID,
  loan_id: null,
  borrower_member_id: BORROWER_ID,
  guarantor_member_id: GUARANTOR_ID,
  amount: "50000.10",
  status: "pledged",
  version: 1,
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Current application record served by GET (queue + drawer). */
  getApplication: () => Record<string, unknown>;
  /** Returns [status, body] for POST .../guarantees. */
  postPledge: (body: Record<string, unknown>) => [number, unknown];
  pledgeBodies: Record<string, unknown>[];
  pledgeHeaders: Record<string, string>[];
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
      await respond(200, state.permissions ?? FULL_PERMISSIONS);
      return;
    }
    if (path === "/products" && method === "GET") {
      await respond(200, [PRODUCT_OUT]);
      return;
    }
    if (path === "/dashboard/summary" && method === "GET") {
      await respond(200, SUMMARY_OUT);
      return;
    }
    if (path === "/members" && method === "GET") {
      await respond(200, { items: [BORROWER_OUT, GUARANTOR_OUT], next_cursor: null });
      return;
    }
    if (path === `/members/${BORROWER_ID}` && method === "GET") {
      await respond(200, BORROWER_OUT);
      return;
    }
    if (path === `/members/${GUARANTOR_ID}` && method === "GET") {
      await respond(200, GUARANTOR_OUT);
      return;
    }
    if (path === "/applications" && method === "GET") {
      const stage = new URL(request.url()).searchParams.get("stage");
      const current = state.getApplication();
      const items = stage === null || current["stage"] === stage ? [current] : [];
      await respond(200, { items, next_cursor: null });
      return;
    }
    if (path === `/applications/${APP_ID}` && method === "GET") {
      await respond(200, state.getApplication());
      return;
    }
    if (path === `/applications/${APP_ID}/guarantees` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.pledgeBodies.push(body);
      state.pledgeHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postPledge(body);
      await respond(status, responseBody);
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

test("happy path: OTP login → guarantors renders SERVER aggregates verbatim → pledge commits exactly ONE POST through the typed confirmation and the record enters the session panel", async ({
  page,
}) => {
  const state: ApiState = {
    getApplication: () => applicationOut(),
    postPledge: () => [201, GUARANTEE_OUT],
    pledgeBodies: [],
    pledgeHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Guarantors" }).click();
  // Guarantor aggregates are SERVER figures rendered verbatim (the
  // trailing cents survive — blocker (a))…
  await expect(page.getByText("KES 1,234,567.10")).toBeVisible();
  // …including the per-guarantor capacity slice.
  await expect(page.getByText("KES 500,000.10")).toBeVisible();

  // Open the pledge drawer from the pledgeable-stage queue.
  await page.getByText("Pledge ›").click();
  const drawer = page.getByRole("dialog", { name: "Pledge guarantee" });
  await expect(drawer.getByText("Jane Wanjiku · M-0001")).toBeVisible();
  await drawer.getByLabel("Guarantor").selectOption(GUARANTOR_ID);
  // The chosen guarantor's SERVER-reported free capacity renders.
  await expect(drawer.getByText(/Server-reported free capacity/)).toBeVisible();
  await drawer.getByLabel("Pledge amount (KES)").fill("50000.10");
  await drawer.getByRole("button", { name: "Pledge…" }).click();

  // Typed confirmation: the write happens only after the byte-identical
  // phrase; the confirm button starts disabled.
  const confirm = page.getByRole("dialog", { name: "Encumber savings" });
  const confirmButton = confirm.getByRole("button", { name: "Pledge guarantee" });
  await expect(confirmButton).toBeDisabled();
  expect(state.pledgeBodies).toHaveLength(0);
  const phrase = APP_ID.slice(0, 8);
  await confirm.getByLabel(`Type "${phrase}" to confirm`).fill(phrase);
  await confirmButton.click();

  // The result panel renders the SERVER record verbatim…
  await expect(drawer.getByText("Guarantee pledged")).toBeVisible();
  await expect(drawer.getByText("KES 50,000.10")).toBeVisible();
  // …the affordance is spent…
  await expect(drawer.getByRole("button", { name: "Pledge…" })).toHaveCount(0);

  // …and exactly ONE write reached the wire: guarantor + decimal-STRING
  // amount only, idempotency key + bearer as headers.
  expect(state.pledgeBodies).toHaveLength(1);
  expect(state.pledgeBodies[0]).toEqual({
    guarantor_member_id: GUARANTOR_ID,
    amount: "50000.10",
  });
  expect(state.pledgeHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.pledgeHeaders[0]?.["authorization"]).toMatch(/^Bearer /);

  // The witnessed record now sits in the session panel.
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(page.getByText("Pledged (unconsented)")).toBeVisible();
  await expect(page.getByRole("button", { name: "Consent…" })).toBeVisible();
});

test("adversarial (money write): stale pledge → 409 conflict banner, EXACTLY ONE write, explicit reload never replays and withdraws the action", async ({
  page,
}) => {
  let stage = "submitted";
  const state: ApiState = {
    getApplication: () => applicationOut({ stage }),
    postPledge: () => [409, { category: "conflict", correlation_id: "corr-e2e-stale" }],
    pledgeBodies: [],
    pledgeHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Guarantors" }).click();
  await page.getByText("Pledge ›").click();
  const drawer = page.getByRole("dialog", { name: "Pledge guarantee" });
  await drawer.getByLabel("Guarantor").selectOption(GUARANTOR_ID);
  await drawer.getByLabel("Pledge amount (KES)").fill("50000.10");
  await drawer.getByRole("button", { name: "Pledge…" }).click();
  const confirm = page.getByRole("dialog", { name: "Encumber savings" });
  const phrase = APP_ID.slice(0, 8);
  await confirm.getByLabel(`Type "${phrase}" to confirm`).fill(phrase);
  await confirm.getByRole("button", { name: "Pledge guarantee" }).click();

  // The conflict banner offers the explicit reload flow; the write was
  // NOT applied and was attempted exactly once (no auto-retry).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.pledgeBodies).toHaveLength(1);

  // Reload fetches the fresh record — approved underneath, no longer
  // pledgeable — and the action is structurally withdrawn; NOTHING was
  // replayed.
  stage = "approved";
  await drawer.getByRole("button", { name: "Reload record" }).click();
  await expect(drawer.getByText(/no longer in a pledgeable stage/)).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Pledge…" })).toHaveCount(0);
  expect(state.pledgeBodies).toHaveLength(1);
});

test("adversarial: deny-by-default — a role without applications:view gets no Guarantors nav entry and an Access denied guard on the direct URL", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: {
      role_id: "66666666-6666-6666-6666-666666666666",
      permissions: [
        { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
      ],
    },
    getApplication: () => applicationOut(),
    postPledge: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    pledgeBodies: [],
    pledgeHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the guarantors entry never mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Guarantors" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard.
  await page.goto("/modules/applications/guarantors");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Open for pledging")).toHaveCount(0);
  await expect(page.getByText("Pledge ›")).toHaveCount(0);
});
