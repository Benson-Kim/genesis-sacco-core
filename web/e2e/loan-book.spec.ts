/**
 * Loan book E2E (P15 exit criterion: per-module happy path + adversarial
 * flows), driven through the REAL production build in a real browser —
 * OTP login UI, session custody, deny-by-default guards, generated
 * client, typed-confirmation disbursement.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies are asserted server-side-of-the-
 * wire, so the single-write double-disbursement proof measures real
 * network effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const LOAN_ID = "cccccccc-1111-2222-3333-444444444444";

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
    { module: "loan_book", can_view: true, can_create: true, can_edit: true, can_approve: true },
    { module: "applications", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "transactions", can_view: true, can_create: true, can_edit: false, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function applicationOut(overrides: Record<string, unknown> = {}) {
  return {
    id: APP_ID,
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
    rate_pct: "12.50",
    purpose: "School fees",
    stage: "approved",
    cover_pct: "120.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 5,
    ...overrides,
  };
}

const LOAN_OUT = {
  id: LOAN_ID,
  application_id: APP_ID,
  member_id: MEMBER_ID,
  product_id: PRODUCT_ID,
  principal: "2000000.00",
  balance: "1234567.10",
  rate_pct: "12.50",
  term_months: 36,
  status: "active",
  classification: "watch",
  days_past_due: 12,
  provision_pct: "5.00",
  penalty_due: "1500.10",
  disbursed_at: "2026-01-15T09:00:00Z",
  closed_at: null,
  version: 7,
};

const SCHEDULE_ROW = {
  installment_no: 1,
  due_date: "2026-02-15",
  principal_due: "50000.10",
  interest_due: "20833.33",
  total_due: "70833.43",
  paid_amount: "0.00",
};

const SUMMARY_OUT = {
  active_loans: 12,
  outstanding_balance: "9876543.10",
  npl_balance: "1111111.10",
  npl_ratio_pct: "12.50",
  par30_balance: "2222222.00",
  par30_ratio_pct: "20.25",
  provisions: "456789.10",
  penalties_due: "9999.10",
  by_classification: [
    { classification: "normal", count: 9, balance: "7000000.00", provisions: "70000.00" },
  ],
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

const MEMBER_OUT = {
  id: MEMBER_ID,
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

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Current application record served by GET (queue + dialog). */
  getApplication: () => Record<string, unknown>;
  /** Returns [status, body] for POST .../disburse. */
  postDisburse: (body: Record<string, unknown>) => [number, unknown];
  disburseBodies: Record<string, unknown>[];
  disburseHeaders: Record<string, string>[];
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
    if (path === "/members" && method === "GET") {
      await respond(200, { items: [MEMBER_OUT], next_cursor: null });
      return;
    }
    if (path === `/members/${MEMBER_ID}` && method === "GET") {
      await respond(200, MEMBER_OUT);
      return;
    }
    if (path === "/portfolio/summary" && method === "GET") {
      await respond(200, SUMMARY_OUT);
      return;
    }
    if (path === "/loans" && method === "GET") {
      await respond(200, { items: [LOAN_OUT], next_cursor: null });
      return;
    }
    if (path === `/loans/${LOAN_ID}` && method === "GET") {
      await respond(200, LOAN_OUT);
      return;
    }
    if (path === `/loans/${LOAN_ID}/schedule` && method === "GET") {
      await respond(200, [SCHEDULE_ROW]);
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
    if (path === `/applications/${APP_ID}/disburse` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.disburseBodies.push(body);
      state.disburseHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postDisburse(body);
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

test("happy path: OTP login → loan book renders SERVER portfolio figures + register verbatim → disbursement commits exactly ONE POST through the typed confirmation", async ({
  page,
}) => {
  const state: ApiState = {
    getApplication: () => applicationOut(),
    postDisburse: () => [
      201,
      {
        loan_id: LOAN_ID,
        txn_id: "eeeeeeee-1111-2222-3333-444444444444",
        txn_ref: "TXN-0001",
        schedule: [SCHEDULE_ROW],
      },
    ],
    disburseBodies: [],
    disburseHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Loan book" }).click();
  // Portfolio aggregates are SERVER figures rendered verbatim…
  await expect(page.getByText("KES 9,876,543.10")).toBeVisible();
  await expect(page.getByText("12.50% of book")).toBeVisible();
  // …and the register balance keeps its trailing cents (blocker (a)).
  await expect(page.getByText("KES 1,234,567.10")).toBeVisible();

  // The approved queue offers the deferred-to-this-batch action.
  await page.getByText("Disburse ›").click();
  const dialog = page.getByRole("dialog", { name: "Disburse loan" });
  await expect(dialog.getByText("KES 250,000.10")).toBeVisible();
  await dialog.getByLabel("Disbursement channel").selectOption("mpesa");
  await dialog.getByRole("button", { name: "Disburse…" }).click();

  // Typed confirmation: the write happens only after the byte-identical
  // phrase; the confirm button starts disabled.
  const confirm = page.getByRole("dialog", { name: "Disburse funds" });
  const confirmButton = confirm.getByRole("button", { name: "Disburse funds" });
  await expect(confirmButton).toBeDisabled();
  expect(state.disburseBodies).toHaveLength(0);
  const phrase = APP_ID.slice(0, 8);
  await confirm.getByLabel(`Type "${phrase}" to confirm`).fill(phrase);
  await confirmButton.click();

  // The result panel renders the SERVER-computed schedule…
  await expect(page.getByText("Disbursed · ref TXN-0001")).toBeVisible();
  await expect(page.getByText("KES 70,833.43")).toBeVisible();
  // …the affordance is spent…
  await expect(dialog.getByRole("button", { name: "Disburse…" })).toHaveCount(0);

  // …and exactly ONE write reached the wire: channel only, idempotency
  // key + bearer as headers.
  expect(state.disburseBodies).toHaveLength(1);
  expect(state.disburseBodies[0]).toEqual({ channel: "mpesa" });
  expect(state.disburseHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.disburseHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("adversarial (money write): stale disbursement → 409 conflict banner, EXACTLY ONE write, explicit reload never replays and withdraws the action", async ({
  page,
}) => {
  let stage = "approved";
  const state: ApiState = {
    getApplication: () => applicationOut({ stage }),
    postDisburse: () => [409, { category: "conflict", correlation_id: "corr-e2e-stale" }],
    disburseBodies: [],
    disburseHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Loan book" }).click();
  await page.getByText("Disburse ›").click();
  const dialog = page.getByRole("dialog", { name: "Disburse loan" });
  await dialog.getByLabel("Disbursement channel").selectOption("bank");
  await dialog.getByRole("button", { name: "Disburse…" }).click();
  const confirm = page.getByRole("dialog", { name: "Disburse funds" });
  const phrase = APP_ID.slice(0, 8);
  await confirm.getByLabel(`Type "${phrase}" to confirm`).fill(phrase);
  await confirm.getByRole("button", { name: "Disburse funds" }).click();

  // The conflict banner offers the explicit reload flow; the write was
  // NOT applied and was attempted exactly once (no auto-retry).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.disburseBodies).toHaveLength(1);

  // Reload fetches the fresh record — disbursed by another operator —
  // and the action is structurally withdrawn; NOTHING was replayed.
  stage = "disbursed";
  await dialog.getByRole("button", { name: "Reload record" }).click();
  await expect(dialog.getByText(/not in the approved stage/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Disburse…" })).toHaveCount(0);
  expect(state.disburseBodies).toHaveLength(1);
});

test("adversarial: deny-by-default — a role without loan_book:view gets no nav entry and an Access denied guard on the direct URL", async ({
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
    postDisburse: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    disburseBodies: [],
    disburseHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the loan-book entry never mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Loan book" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard.
  await page.goto("/modules/loan_book");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Disbursement queue")).toHaveCount(0);
  await expect(page.getByText("Disburse ›")).toHaveCount(0);
});
