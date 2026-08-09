/**
 * Dashboard E2E (P15 exit criterion backfill — issue #30 T1: batch-1
 * modules pre-dated the e2e harness and were never back-filled):
 * happy path + forbidden-role adversarial flow, driven through the REAL
 * production build in a real browser — OTP login UI, session custody,
 * generated client, Zod boundary, string-only money formatters.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin), extending the existing harness pattern (never rebuilt).
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
    { module: "members", can_view: true, can_create: true, can_edit: true, can_approve: true },
    { module: "loan_book", can_view: true, can_create: true, can_edit: true, can_approve: true },
    {
      module: "transactions",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
    {
      module: "applications",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

/** A role with NONE of the four dashboard slice grants — the server's
 * RequireAnyPermission (api/dashboard.py) refuses the summary outright. */
const NO_SLICE_PERMISSIONS = {
  role_id: "66666666-6666-6666-6666-666666666666",
  permissions: [
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** Canonical server shapes ONLY (the retrofitted Zod boundary rejects
 * anything else): str(Decimal) two-place aggregates, the SQL bare "0"
 * for an empty aggregate, datetime.isoformat() for as_of. */
const SUMMARY_OUT = {
  as_of: "2026-08-04T12:00:00+00:00",
  members: { active_members: 87, by_type: [] },
  deposits: { total_deposits: "1234567.10", total_share_capital: "250000.00" },
  loan_book: {
    active_loans: 12,
    outstanding_balance: "5000000.00",
    npl_balance: "0",
    npl_ratio_pct: "0.00",
    par30_balance: "160000.00",
    par30_ratio_pct: "3.20",
    provisions: "7500.00",
    penalties_due: "0",
    by_classification: [
      { classification: "Watch", count: 2, balance: "150000.10", provisions: "7500.00" },
    ],
  },
  guarantors: {
    active_guarantees: 0,
    total_pledged: "0",
    distinct_guarantors: 0,
    guarantors: [],
  },
  monthly_flows: [
    // A reversal-only month legitimately nets NEGATIVE
    // (MONTHLY_FLOWS_SQL) — the sign must render verbatim.
    { month: "2026-06", deposits: "-200000.00", disbursements: "0" },
    { month: "2026-07", deposits: "450000.10", disbursements: "150000.00" },
  ],
  pipeline: [{ stage: "Review", count: 4 }],
};

interface ApiState {
  permissions: unknown;
  /** Returns [status, body] for GET /dashboard/summary. */
  getSummary: () => [number, unknown];
  summaryCalls: number;
}

/** Browser-boundary API mock with CORS handling (harness pattern). */
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
    if (path === "/dashboard/summary" && method === "GET") {
      state.summaryCalls += 1;
      const [status, body] = state.getSummary();
      await respond(status, body);
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

test("happy path: OTP login → dashboard renders every server figure VERBATIM (string-only formatting; the reversal-month sign survives)", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: FULL_PERMISSIONS,
    getSummary: () => [200, SUMMARY_OUT],
    summaryCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Stat cards: KES-labelled decimal strings byte-identical to the wire
  // (blocker (a) — trailing cents survive because nothing is coerced).
  await expect(page.getByText("KES 1,234,567.10")).toBeVisible();
  await expect(page.getByText("KES 5,000,000.00")).toBeVisible();
  await expect(page.getByText("3.20%")).toBeVisible();
  await expect(page.getByText("87", { exact: true })).toBeVisible();

  // Monthly flows: the reversal-only month renders its NEGATIVE server
  // figure verbatim; the SQL bare "0" renders as "0", never re-scaled.
  await expect(page.getByRole("cell", { name: "-200,000.00" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "450,000.10" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "0", exact: true }).first()).toBeVisible();

  // Portfolio classification + pipeline are the server's rows.
  await expect(page.getByRole("cell", { name: "150,000.10" })).toBeVisible();
  await expect(page.getByText("Review")).toBeVisible();

  // The as-of stamp rendered (fmtDateTime on the canonical timestamp).
  await expect(page.getByText(/As of /)).toBeVisible();
});

test("adversarial forbidden-role: no slice grant → the server's 403 renders the least-disclosure banner; NO figure is fabricated or leaked", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: NO_SLICE_PERMISSIONS,
    getSummary: () => [403, { category: "forbidden", correlation_id: "corr-e2e-dash403" }],
    summaryCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Sanitized verdict ONLY: per-status title + correlation id (gate 1.6).
  await expect(page.getByText("Not permitted.")).toBeVisible();
  await expect(page.getByText("ref: corr-e2e-dash403")).toBeVisible();
  // The raw server category never renders (no capability probing).
  await expect(page.getByText(/forbidden/i)).toHaveCount(0);
  // No money figure exists anywhere on the page — nothing fabricated,
  // nothing cached from another role's view.
  await expect(page.getByText(/KES/)).toHaveCount(0);
  // Under-privilege is NOT session death: the shell stays mounted with
  // the role's own granted navigation (Settings), no /login redirect.
  await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();
});
