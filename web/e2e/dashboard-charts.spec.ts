/**
 * Dashboard charts E2E (issue #32 / #31 batch 5, U3+X4 — the recorded
 * route (a) design decision): the four affordances (KPI sparklines,
 * deposits-vs-disbursements grouped bars, performing donut,
 * classification progress bars) driven through the REAL production
 * build in a real browser.
 *
 * Proofs at the browser boundary:
 * - every rendered geometry is the SERVER's integer percentage (the
 *   inline style carries the wire value verbatim — no client-side
 *   money math ever ran);
 * - the scale caption renders the server's axis_max VERBATIM through
 *   the string-only formatter;
 * - charts are aria-hidden SUPPLEMENTS — the sibling tables/stat text
 *   keep carrying the signed figures of record (colour-never-alone);
 * - client hygiene: NO third-party request leaves the page (the gate
 *   that bans chart libraries) — every request stays on the app or
 *   the API origin;
 * - a summary WITHOUT the charts slice renders the tables honestly
 *   with ZERO chart geometry (nothing is fabricated client-side).
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

/** Canonical server shapes only; the charts slice carries the
 * SERVER-computed integer percentages of the recorded route (a). */
const SUMMARY_WITH_CHARTS = {
  as_of: "2026-08-04T12:00:00+00:00",
  members: { active_members: 87, by_type: [] },
  deposits: { total_deposits: "1234567.10", total_share_capital: "250000.00" },
  loan_book: {
    active_loans: 12,
    outstanding_balance: "5000000.00",
    npl_balance: "150000.00",
    npl_ratio_pct: "3.00",
    par30_balance: "160000.00",
    par30_ratio_pct: "3.20",
    provisions: "7500.00",
    penalties_due: "0",
    by_classification: [
      { classification: "watch", count: 2, balance: "150000.10", provisions: "7500.00" },
    ],
  },
  guarantors: {
    active_guarantees: 0,
    total_pledged: "0",
    distinct_guarantors: 0,
    guarantors: [],
  },
  monthly_flows: [
    // A reversal-only month legitimately nets NEGATIVE — the TABLE
    // stays the signed truth; its chart bar clamps to zero.
    { month: "2026-06", deposits: "-200000.00", disbursements: "0" },
    { month: "2026-07", deposits: "450000.10", disbursements: "150000.00" },
  ],
  pipeline: [{ stage: "Review", count: 4 }],
  charts: {
    flows: {
      months: [
        { month: "2026-06", deposits_pct: 0, disbursements_pct: 0 },
        { month: "2026-07", deposits_pct: 100, disbursements_pct: 33 },
      ],
      axis_max: "450000.10",
    },
    portfolio: {
      performing_pct: 97,
      npl_pct: 3,
      classification: [{ classification: "watch", pct: 3 }],
    },
  },
};

/** The same summary WITHOUT the charts slice (older server / no grant
 * on the parent slices): geometry must be ABSENT, never fabricated. */
const SUMMARY_WITHOUT_CHARTS = { ...SUMMARY_WITH_CHARTS, charts: null };

interface ApiState {
  summary: unknown;
}

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
      await respond(200, FULL_PERMISSIONS);
      return;
    }
    if (path === "/dashboard/summary" && method === "GET") {
      await respond(200, state.summary);
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

test("the four #32 affordances render the SERVER's geometry verbatim; charts stay aria-hidden supplements beside the tables of record; NO third-party request leaves the page", async ({
  page,
}) => {
  // Client-hygiene capture: every request the page makes, from the
  // first navigation on — the gate that bans third-party chart
  // libraries measures the wire, not the import graph.
  const requestUrls: string[] = [];
  page.on("request", (request) => {
    requestUrls.push(request.url());
  });

  await mockApi(page, { summary: SUMMARY_WITH_CHARTS });
  await login(page);

  // 1) KPI sparklines on the two cards whose series the contract
  // actually carries, with their honest captions.
  await expect(page.getByTestId("sparkline-gold")).toBeAttached();
  await expect(page.getByTestId("sparkline-navy")).toBeAttached();
  await expect(page.getByText("monthly deposit flows")).toBeVisible();
  await expect(page.getByText("monthly disbursements")).toBeVisible();

  // 2) Grouped bars: the inline style carries the SERVER's integer
  // percentage VERBATIM (100/33; the reversal-only month clamps to 0
  // server-side) — no client-side scale computation exists.
  await expect(page.getByTestId("flows-bar-chart")).toBeAttached();
  await expect(page.getByTestId("bar-dep-2026-07")).toHaveAttribute("style", /height:\s*100%/);
  await expect(page.getByTestId("bar-dis-2026-07")).toHaveAttribute("style", /height:\s*33%/);
  await expect(page.getByTestId("bar-dep-2026-06")).toHaveAttribute("style", /height:\s*0%/);
  // The scale caption is the server's axis_max VERBATIM (string-only
  // formatting — trailing cents survive).
  await expect(page.getByText("KES 450,000.10")).toBeVisible();

  // 3) Performing donut: the ring geometry is the server's integer and
  // the percent TEXT beside it is the accessible carrier.
  await expect(page.getByTestId("performing-donut")).toBeAttached();
  await expect(page.getByTestId("performing-donut")).toHaveAttribute(
    "style",
    /conic-gradient\(var\(--gold\) 97%/,
  );
  await expect(page.getByText("Non-performing")).toBeVisible();

  // 4) Classification progress bars: name + percent TEXT accompany the
  // track (colour-never-alone); the fill width is the wire integer.
  await expect(page.getByTestId("classification-bars")).toBeAttached();
  await expect(page.getByTestId("cls-fill-watch")).toHaveAttribute("style", /width:\s*3%/);

  // A11y: every geometry container is a HIDDEN supplement…
  for (const id of ["flows-bar-chart", "performing-donut"]) {
    await expect(page.getByTestId(id)).toHaveAttribute("aria-hidden", "true");
  }
  // …while the tables/stat text stay the figures of record — the
  // reversal month's SIGN renders verbatim in the flows TABLE (the
  // table uses the unlabelled fmtAmount; the KES label lives on the
  // stat cards).
  await expect(page.getByRole("cell", { name: "-200,000.00" })).toBeVisible();
  await expect(page.getByText("KES 1,234,567.10")).toBeVisible();

  // Client hygiene: nothing left the page except the app itself and
  // the (mocked) API origin — a third-party chart CDN would fail this.
  expect(requestUrls.length).toBeGreaterThan(0);
  for (const url of requestUrls) {
    expect(url).toMatch(/^http:\/\/localhost(?::\d+)?\//);
  }
});

test("a summary WITHOUT the charts slice renders the tables honestly with ZERO chart geometry — nothing is fabricated client-side", async ({
  page,
}) => {
  await mockApi(page, { summary: SUMMARY_WITHOUT_CHARTS });
  await login(page);

  // The figures of record are all there…
  await expect(page.getByText("KES 1,234,567.10")).toBeVisible();
  await expect(page.getByRole("cell", { name: "-200,000.00" })).toBeVisible();

  // …and NO geometry exists anywhere: absent slice, absent charts.
  await expect(page.getByTestId("sparkline-gold")).toHaveCount(0);
  await expect(page.getByTestId("sparkline-navy")).toHaveCount(0);
  await expect(page.getByTestId("flows-bar-chart")).toHaveCount(0);
  await expect(page.getByTestId("performing-donut")).toHaveCount(0);
  await expect(page.getByTestId("classification-bars")).toHaveCount(0);
});
