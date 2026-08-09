/**
 * Branches + accounting-periods E2E (issue #31 batch 4 — P13.6/P12.5;
 * audit #30 A5), driven through the REAL production build in a real
 * browser — OTP login UI, session custody, deny-by-default guards,
 * generated client, the typed close confirmation and the A5 period-
 * context note beside the transactions register.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin, the shared harness): request counts/bodies/headers are
 * asserted server-side-of-the-wire, so the single-write proofs (the
 * branch create body is exactly {name}; the close body is exactly
 * {year, month} — key-exact, no money parameter exists anywhere on
 * either contract), the Idempotency-Key HEADER custody and the empty
 * write query strings measure real network effects, not component
 * internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";

/** ConfirmDangerModal phrase for the July 2026 close. */
const PHRASE = "2026-07";

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

/** Everything this batch's consoles can use: settings CRUD (branches),
 * transactions view+approve (periods), the people-edit grants. */
const FULL_PERMISSIONS = {
  role_id: "77777777-7777-7777-7777-777777777777",
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: false },
    {
      module: "transactions",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: true,
    },
    {
      module: "access_control",
      can_view: true,
      can_create: false,
      can_edit: true,
      can_approve: false,
    },
    { module: "members", can_view: true, can_create: false, can_edit: true, can_approve: false },
  ],
};

const BRANCH_OUT = {
  id: BRANCH_ID,
  name: "Nairobi West",
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

const CREATED_BRANCH = {
  id: "dddddddd-eeee-ffff-0000-222222222222",
  name: "Kisumu Central",
  version: 1,
  created_at: "2026-08-06T10:00:00+00:00",
};

/** June 2026 already closed; the operator closes July. */
const CLOSED_JUNE = {
  id: "pppppppp-aaaa-bbbb-cccc-111111111111",
  period_start: "2026-06-01",
  period_end: "2026-06-30",
  status: "closed",
  closed_at: "2026-07-01T08:00:00+00:00",
  version: 1,
};

const CLOSED_JULY = {
  id: "pppppppp-aaaa-bbbb-cccc-222222222222",
  period_start: "2026-07-01",
  period_end: "2026-07-31",
  status: "closed",
  closed_at: "2026-08-06T10:00:00+00:00",
  version: 1,
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  createBodies: Record<string, unknown>[];
  createHeaders: Record<string, string>[];
  createUrls: string[];
  closeBodies: Record<string, unknown>[];
  closeHeaders: Record<string, string>[];
  closeUrls: string[];
  /** EVERY request touching /branches (deny-by-default proof). */
  branchesCalls: number;
  /** EVERY request touching /accounting-periods (deny-by-default proof). */
  periodsCalls: number;
}

/** Browser-boundary API mock with CORS handling and write capture. */
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

    if (path.startsWith("/branches") || path.startsWith("/jobs/branch-backfill")) {
      state.branchesCalls += 1;
    }
    if (path.startsWith("/accounting-periods")) state.periodsCalls += 1;

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
    if (path === "/branches" && method === "GET") {
      await respond(200, { items: [BRANCH_OUT], next_cursor: null });
      return;
    }
    if (path === "/branches" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createBodies.push(body);
      state.createHeaders.push(await request.allHeaders());
      state.createUrls.push(request.url());
      await respond(201, CREATED_BRANCH);
      return;
    }
    if (path === `/branches/${BRANCH_ID}` && method === "GET") {
      await respond(200, BRANCH_OUT);
      return;
    }
    if (path === "/accounting-periods" && method === "GET") {
      const closed =
        state.closeBodies.length > 0 ? [CLOSED_JULY, CLOSED_JUNE] : [CLOSED_JUNE];
      await respond(200, { items: closed, next_cursor: null });
      return;
    }
    if (path === "/accounting-periods/close" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.closeBodies.push(body);
      state.closeHeaders.push(await request.allHeaders());
      state.closeUrls.push(request.url());
      await respond(201, CLOSED_JULY);
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

function freshState(): ApiState {
  return {
    createBodies: [],
    createHeaders: [],
    createUrls: [],
    closeBodies: [],
    closeHeaders: [],
    closeUrls: [],
    branchesCalls: 0,
    periodsCalls: 0,
  };
}

test("happy path: OTP login → branches register + create (ONE wire POST, body exactly {name}, Idempotency-Key as a HEADER, empty query) → periods register + typed close (ONE wire POST, body exactly {year, month}) → the A5 period-context note renders beside the transactions register", async ({
  page,
}) => {
  const state = freshState();
  await mockApi(page, state);
  await login(page);

  // ---- Branches register + create -------------------------------
  await page.getByRole("link", { name: "Branches" }).click();
  await expect(page.getByText("Branch registry")).toBeVisible();
  await expect(page.getByText("Nairobi West")).toBeVisible();

  await page.getByRole("button", { name: "+ Register branch" }).click();
  await page.getByLabel("Branch name").fill("Kisumu Central");
  await page.getByRole("button", { name: "Register branch", exact: true }).click();

  // The SERVER's record renders verbatim in the result panel… (exact:
  // the announcer's "Branch registered." live-region copy also mounts)
  await expect(page.getByText("Branch registered", { exact: true })).toBeVisible();
  await expect(page.getByText("Kisumu Central")).toBeVisible();
  // …and exactly ONE write reached the wire: key-exact body, secrets
  // as HEADERS, the query string EMPTY (no money parameter exists on
  // this contract for a caller to supply).
  expect(state.createBodies).toHaveLength(1);
  expect(state.createBodies[0]).toEqual({ name: "Kisumu Central" });
  expect(state.createHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.createHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.createUrls[0] ?? "").search).toBe("");
  // Dismiss the (spent) drawer via the result panel's Close button —
  // the submit unmounted with the SPENT form, so focus (and Escape)
  // no longer live inside the dialog (the reports.spec pattern).
  await page
    .getByRole("dialog", { name: "Register branch" })
    .getByRole("status")
    .getByRole("button", { name: "Close" })
    .click();
  await expect(page.getByRole("dialog", { name: "Register branch" })).toHaveCount(0);

  // ---- Periods register + typed close ---------------------------
  await page.getByRole("link", { name: "Accounting periods" }).click();
  await expect(page.getByText("Closed accounting periods")).toBeVisible();
  await expect(page.getByText("2026-06-01 → 2026-06-30")).toBeVisible();
  await expect(page.getByText("Closed", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Close period…" }).click();
  const dialog = page.getByRole("dialog", { name: "Close accounting period" });
  await dialog.getByLabel("Year").fill("2026");
  await dialog.getByLabel("Month").selectOption({ label: "July" });
  await dialog.getByRole("button", { name: "Close period…" }).click();

  // Typed confirmation: the danger button starts DISABLED; nothing has
  // reached the wire yet.
  const confirmButton = page.getByRole("button", { name: "Close period", exact: true });
  await expect(confirmButton).toBeDisabled();
  expect(state.closeBodies).toHaveLength(0);
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await confirmButton.click();

  // The SERVER's period row renders verbatim in the result panel…
  // (scoped to the dialog: the invalidated register behind the modal
  // refetches and now carries the July row too)
  await expect(dialog.getByText("Period closed · books frozen")).toBeVisible();
  await expect(dialog.getByText("2026-07-01 → 2026-07-31")).toBeVisible();
  // …and exactly ONE write reached the wire: the body carries the
  // calendar month ONLY (bounds/elapsed rules are the server's).
  expect(state.closeBodies).toHaveLength(1);
  expect(state.closeBodies[0]).toEqual({ year: 2026, month: 7 });
  expect(state.closeHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.closeHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.closeUrls[0] ?? "").search).toBe("");

  // Dismiss the (spent) dialog via the result panel's Close button
  // before navigating on (same focus reasoning as the drawer above).
  await dialog.getByRole("status").getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Close accounting period" })).toHaveCount(0);

  // ---- A5: period context NEXT TO the figures operators read ----
  await page.getByRole("link", { name: "Transactions", exact: true }).click();
  await expect(page.getByText(/books closed through/)).toBeVisible();
  await expect(page.getByText("2026-07-31")).toBeVisible();
  await expect(page.getByRole("link", { name: "View period register" })).toBeVisible();
});

test("adversarial: deny-by-default — a role without settings/transactions gets NO Branches or Accounting-periods nav entries, Access denied on both direct URLs and ZERO branches/periods calls", async ({
  page,
}) => {
  const state = freshState();
  state.permissions = {
    role_id: "66666666-6666-6666-6666-666666666666",
    permissions: [
      { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
    ],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: neither entry mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Branches" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Accounting periods" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guards — and the
  // stripped role triggers ZERO /branches and /accounting-periods
  // requests (the A5 note on other routes self-gates too).
  await page.goto("/modules/settings/branches");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Branch registry")).toHaveCount(0);

  await page.goto("/modules/transactions/periods");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Closed accounting periods")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Close period…" })).toHaveCount(0);

  expect(state.branchesCalls).toBe(0);
  expect(state.periodsCalls).toBe(0);
});
