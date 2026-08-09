/**
 * Applications + Committee E2E (P15 exit criterion: per-module happy
 * path + one adversarial flow), driven through the REAL production
 * build in a real browser — OTP login UI, session custody,
 * deny-by-default guards, generated client, maker-checker voting.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies are asserted server-side-of-the-
 * wire, so the single-write and separation-of-duties proofs measure
 * real network effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";

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
    {
      module: "applications",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
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
    stage: "submitted",
    cover_pct: "120.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 3,
    ...overrides,
  };
}

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
  /** Current application record served by GET (list + detail). */
  getApplication: () => Record<string, unknown>;
  /** Returns [status, body] for POST /applications. */
  postCreate: (body: Record<string, unknown>) => [number, unknown];
  /** Returns [status, body] for POST .../transition. */
  postTransition: (body: Record<string, unknown>) => [number, unknown];
  createBodies: Record<string, unknown>[];
  createHeaders: Record<string, string>[];
  transitionBodies: Record<string, unknown>[];
  voteBodies: Record<string, unknown>[];
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
    if (path === "/applications" && method === "GET") {
      const stage = new URL(request.url()).searchParams.get("stage");
      const current = state.getApplication();
      const items = stage === null || current["stage"] === stage ? [current] : [];
      await respond(200, { items, next_cursor: null });
      return;
    }
    if (path === "/applications" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createBodies.push(body);
      state.createHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postCreate(body);
      await respond(status, responseBody);
      return;
    }
    if (path === `/applications/${APP_ID}` && method === "GET") {
      await respond(200, state.getApplication());
      return;
    }
    if (path === `/applications/${APP_ID}/transition` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.transitionBodies.push(body);
      const [status, responseBody] = state.postTransition(body);
      await respond(status, responseBody);
      return;
    }
    if (path === `/applications/${APP_ID}/vote` && method === "POST") {
      state.voteBodies.push(request.postDataJSON() as Record<string, unknown>);
      await respond(200, { approvals: 1, rejections: 0, decision: null, stage: "committee" });
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

test("happy path: OTP login → applications register renders verbatim money → intake commits ONE POST with a string amount → detail shows server-computed figures", async ({
  page,
}) => {
  const state: ApiState = {
    getApplication: () => applicationOut(),
    postCreate: (body) => [
      201,
      applicationOut({ amount: body["amount"], term_months: body["term_months"], version: 1 }),
    ],
    postTransition: () => [409, { category: "conflict", correlation_id: "corr-unused" }],
    createBodies: [],
    createHeaders: [],
    transitionBodies: [],
    voteBodies: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Applications" }).click();
  // Decimal strings render byte-identically (blocker (a)).
  await expect(page.getByText("KES 250,000.10")).toBeVisible();

  await page.getByRole("button", { name: "+ New application" }).click();
  const dialog = page.getByRole("dialog", { name: "New loan application" });
  await dialog.getByLabel("Member").selectOption(MEMBER_ID);
  await dialog.getByLabel("Loan product").selectOption(PRODUCT_ID);
  await dialog.getByLabel("Amount (KES)").fill("250000.10");
  await dialog.getByLabel("Term (months)").fill("24");
  await dialog.getByRole("button", { name: "Submit application" }).click();

  // The created record's detail opens with SERVER-computed figures
  // (scoped to the drawer — the register behind it also renders pills).
  const detail = page.getByRole("dialog", { name: "Application detail" });
  await expect(detail.getByText("Security cover")).toBeVisible();
  await expect(detail.getByText("120.00%")).toBeVisible();
  await expect(detail.getByText("12.50% (product-set)")).toBeVisible();

  // Exactly ONE create reached the wire; the amount is the typed STRING
  // and the write carries Idempotency-Key + Bearer headers.
  expect(state.createBodies).toHaveLength(1);
  expect(state.createBodies[0]?.["amount"]).toBe("250000.10");
  expect(state.createBodies[0]?.["member_id"]).toBe(MEMBER_ID);
  expect(state.createHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.createHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("adversarial (separation of duties): the officer who recommends to committee gets NO vote affordances on the committee screen — ZERO vote requests possible", async ({
  page,
}) => {
  let current = applicationOut({ stage: "appraisal", version: 4 });
  const state: ApiState = {
    getApplication: () => current,
    postCreate: () => [409, { category: "conflict", correlation_id: "corr-unused" }],
    postTransition: (body) => {
      if (body["target"] === "committee") {
        current = applicationOut({ stage: "committee", version: 5 });
        return [200, current];
      }
      return [409, { category: "conflict", correlation_id: "corr-e2e-bad-move" }];
    },
    createBodies: [],
    createHeaders: [],
    transitionBodies: [],
    voteBodies: [],
  };
  await mockApi(page, state);
  await login(page);

  // The signed-in operator recommends the application to committee…
  await page.getByRole("link", { name: "Applications" }).click();
  await page.getByText("KES 250,000.10").click();
  await page.getByRole("button", { name: "Recommend to committee" }).click();
  await expect(page.getByText("Recommended to committee.")).toBeVisible();
  expect(state.transitionBodies).toHaveLength(1);
  expect(state.transitionBodies[0]).toMatchObject({ target: "committee", version: 4 });
  await page.getByRole("button", { name: "Close" }).click();

  // …then opens the committee screen: the maker-checker panel withholds
  // THEIR OWN vote controls structurally (no override exists).
  await page.getByRole("link", { name: "Credit committee" }).click();
  await expect(page.getByText(/Approval requires a DIFFERENT/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Vote approve" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Vote reject" })).toHaveCount(0);

  // No vote request ever reached the wire.
  expect(state.voteBodies).toHaveLength(0);
});

test("adversarial: stale stage move → 409 conflict banner, EXACTLY ONE write, explicit reload never replays", async ({
  page,
}) => {
  let version = 3;
  const state: ApiState = {
    getApplication: () => applicationOut({ stage: "submitted", version }),
    postCreate: () => [409, { category: "conflict", correlation_id: "corr-unused" }],
    postTransition: () => [409, { category: "conflict", correlation_id: "corr-e2e-stale" }],
    createBodies: [],
    createHeaders: [],
    transitionBodies: [],
    voteBodies: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Applications" }).click();
  await page.getByText("KES 250,000.10").click();
  await page.getByRole("button", { name: "Move to appraisal" }).click();

  // The conflict banner offers the explicit reload flow; the move was
  // NOT applied and was attempted exactly once (no auto-retry).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.transitionBodies).toHaveLength(1);
  expect(state.transitionBodies[0]).toMatchObject({ target: "appraisal", version: 3 });

  // Reload fetches the fresh record (moved server-side, v4) and NEVER
  // replays the stale submission.
  version = 4;
  const detail = page.getByRole("dialog", { name: "Application detail" });
  await detail.getByRole("button", { name: "Reload record" }).click();
  await expect(detail.getByText("Record reloaded — re-check the stage before acting again.")).toBeVisible();
  expect(state.transitionBodies).toHaveLength(1);
});

test("adversarial: deny-by-default — a role without applications:view gets no nav entries and an Access denied guard", async ({
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
    postCreate: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    postTransition: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    createBodies: [],
    createHeaders: [],
    transitionBodies: [],
    voteBodies: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: neither entry mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Applications" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Credit committee" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guards.
  await page.goto("/modules/applications");
  await expect(page.getByText("Access denied")).toBeVisible();
  await page.goto("/modules/applications/committee");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByRole("button", { name: "+ New application" })).toHaveCount(0);
});
