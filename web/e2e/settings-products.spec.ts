/**
 * Settings + Products E2E (P15 exit criterion: per-module happy path +
 * one adversarial flow), driven through the REAL production build in a
 * real browser — OTP login UI, session custody, deny-by-default guards,
 * generated client, typed-confirmation money writes.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies are asserted server-side-of-the-
 * wire, so double-submit and 409 single-attempt proofs measure real
 * network effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";

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
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

const NO_SETTINGS_PERMISSIONS = {
  role_id: "66666666-6666-6666-6666-666666666666",
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function settingsOut(overrides: Record<string, unknown> = {}) {
  return {
    configured: true,
    version: 5,
    corrupt_keys: [],
    deposit_interest_annual_rate_pct: "9.00",
    dividend_rate_pct: "14.00",
    deposit_rebate_rate_pct: null,
    penalty_rate_pct_per_month: "1.50",
    penalty_grace_days: 5,
    penalty_charged_on: "instalment_in_arrears",
    loan_interest_method: "reducing_balance",
    loan_interest_basis: "actual_365",
    loan_rate_bands: null,
    min_share_capital: "15000.00",
    registration_fee: "1000.00",
    min_monthly_contribution: "2000.00",
    max_member_exposure: "2000000.00",
    dormancy_period_months: 6,
    financial_year_end_month: 12,
    exit_notice_period_days: 30,
    exit_fee: "500.00",
    committee_size: 5,
    committee_quorum: 3,
    approval_bands: null,
    ...overrides,
  };
}

const PRODUCT_OUT = {
  id: "44444444-4444-4444-4444-444444444444",
  name: "Development Loan",
  rate_pct: "12.50",
  deposit_multiplier: "3.00",
  max_term_months: 24,
  guarantors_required: 2,
  active: true,
  version: 1,
};

interface ApiState {
  permissions: unknown;
  getSettings: () => unknown;
  /** Returns [status, body] for PUT /settings. */
  putSettings: (body: Record<string, unknown>) => [number, unknown];
  putBodies: Record<string, unknown>[];
  putHeaders: Record<string, string>[];
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
    if (path === "/settings" && method === "GET") {
      await respond(200, state.getSettings());
      return;
    }
    if (path === "/settings" && method === "PUT") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.putBodies.push(body);
      state.putHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.putSettings(body);
      await respond(status, responseBody);
      return;
    }
    if (path === "/products" && method === "GET") {
      await respond(200, [PRODUCT_OUT]);
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

test("happy path: OTP login → settings tabs render verbatim money strings → typed-confirmation save commits ONE versioned write", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: FULL_PERMISSIONS,
    getSettings: () => settingsOut(),
    putSettings: (body) => [
      200,
      settingsOut({ version: 6, dividend_rate_pct: body["dividend_rate_pct"] }),
    ],
    putBodies: [],
    putHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Settings" }).click();
  const dividend = page.getByLabel("Dividend on shares (% p.a.)");
  // Decimal strings render byte-identically (blocker (a)); the tab
  // opens READ-ONLY (#35 item 3) — Edit is the explicit way in.
  await expect(dividend).toHaveValue("14.00");
  await expect(dividend).toBeDisabled();
  await expect(page.getByLabel("Interest on deposits (% p.a.)")).toHaveValue("9.00");
  await page.getByRole("button", { name: "Edit interest rules" }).click();

  await dividend.fill("15.00");
  await page.getByRole("button", { name: "Save interest rules" }).click();
  const dialog = page.getByRole("dialog", { name: "Apply interest rules" });
  // Typed confirmation gates the money write.
  await expect(dialog.getByRole("button", { name: "Apply interest rules" })).toBeDisabled();
  await dialog.getByLabel('Type "interest" to confirm').fill("interest");
  await dialog.getByRole("button", { name: "Apply interest rules" }).click();

  // Exactly one PUT with the loaded version, the verbatim string and an
  // Idempotency-Key header.
  await expect(dividend).toHaveValue("15.00");
  expect(state.putBodies).toHaveLength(1);
  expect(state.putBodies[0]?.["version"]).toBe(5);
  expect(state.putBodies[0]?.["dividend_rate_pct"]).toBe("15.00");
  expect(state.putHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.putHeaders[0]?.["authorization"]).toMatch(/^Bearer /);

  // Products tab renders the P9 contract verbatim.
  await page.getByRole("tab", { name: "Loan products" }).click();
  await expect(page.getByText("Development Loan").first()).toBeVisible();
  await expect(page.getByLabel("Rate (% p.a.)")).toHaveValue("12.50");
});

test("adversarial: stale settings edit → 409 conflict banner, EXACTLY ONE write, explicit reload never replays", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: FULL_PERMISSIONS,
    getSettings: () => settingsOut(),
    putSettings: () => [409, { category: "conflict", correlation_id: "corr-e2e-stale" }],
    putBodies: [],
    putHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Settings" }).click();
  const dividend = page.getByLabel("Dividend on shares (% p.a.)");
  await expect(dividend).toHaveValue("14.00");
  await page.getByRole("button", { name: "Edit interest rules" }).click();
  await dividend.fill("15.00");
  await page.getByRole("button", { name: "Save interest rules" }).click();
  const dialog = page.getByRole("dialog", { name: "Apply interest rules" });
  await dialog.getByLabel('Type "interest" to confirm').fill("interest");
  await dialog.getByRole("button", { name: "Apply interest rules" }).click();

  // The conflict banner offers the explicit reload flow; the write was
  // NOT applied and was attempted exactly once (no auto-retry).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.putBodies).toHaveLength(1);

  // Reload fetches the fresh record (v6, server-side change) and NEVER
  // replays the stale submission.
  state.getSettings = () => settingsOut({ version: 6, dividend_rate_pct: "16.00" });
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByLabel("Dividend on shares (% p.a.)")).toHaveValue("16.00");
  expect(state.putBodies).toHaveLength(1);
});

test("adversarial: deny-by-default — a role without settings:view gets no nav entry and an Access denied guard", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: NO_SETTINGS_PERMISSIONS,
    getSettings: () => settingsOut(),
    putSettings: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    putBodies: [],
    putHeaders: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: no Settings entry mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard.
  await page.goto("/modules/settings");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByLabel("Dividend on shares (% p.a.)")).toHaveCount(0);
});
