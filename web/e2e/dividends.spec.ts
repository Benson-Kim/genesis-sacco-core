/**
 * Dividends E2E (issue #31 batch 2 — the P13.11 lifecycle console),
 * driven through the REAL production build in a real browser — OTP
 * login UI, session custody, deny-by-default guards, generated client,
 * the maker-checker distribution dialog and its typed confirmation.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin, the shared harness): request counts/bodies/headers are
 * asserted server-side-of-the-wire, so the single-write proof (the
 * distribution body carries {batch_size: 200} ONLY — the SERVER's own
 * documented default, never an operator value; key-exact), the
 * Idempotency-Key HEADER custody and the post-409 key ROTATION proof
 * measure real network effects, not component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const DECL_ID = "dddddddd-aaaa-bbbb-cccc-111111111111";
/** The declarer the SERVER attributes (issue #31 ledger (a).4 — the
 * 0020 requested_by column on the read contract): a DIFFERENT officer
 * than the signed-in ADMIN, so the SoD panel arms the checker side on
 * server truth. */
const DECLARER_ID = "88888888-8888-8888-8888-888888888888";

/** ConfirmDangerModal phrase for the distribution (record id prefix). */
const PHRASE = DECL_ID.slice(0, 8);

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
      module: "transactions",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

/** The committee-APPROVED payout snapshot awaiting distribution. */
const APPROVED_DECLARATION = {
  id: DECL_ID,
  fy_start: "2025-07-01",
  fy_end: "2026-06-30",
  dividend_rate_pct: "14.00",
  rebate_rate_pct: "9.00",
  eligible_members: 42,
  total_share_basis: "500000.00",
  total_deposit_basis: "1200000.10",
  total_dividend: "70000.10",
  total_rebate: "108000.00",
  total_payout: "178000.10",
  status: "approved",
  // Declarer attribution (issue #31 ledger (a).4): server truth —
  // nullable-not-optional on the contract; a missing key refuses to
  // parse at the client's Zod boundary.
  requested_by: DECLARER_ID,
  decided_at: "2026-08-02T10:00:00+00:00",
  distributed_at: null,
  version: 7,
  created_at: "2026-08-01T09:00:00+00:00",
};

/** The completed run report — server figures verbatim, incl. the
 * issue-#19 unclaimed disposition (a mid-run exit). */
const COMPLETE_RUN = {
  scanned: 42,
  claimed: 41,
  skipped_zero: 0,
  skipped_claimed: 0,
  unclaimed: 1,
  dividend_total: "70000.10",
  rebate_total: "108000.00",
  payout_total: "178000.10",
  pending_members: 0,
  batches: 1,
  status: "distributed",
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Returns [status, body] for the distribution POST. */
  postDistribution: (body: Record<string, unknown>) => [number, unknown];
  runBodies: Record<string, unknown>[];
  runHeaders: Record<string, string>[];
  runUrls: string[];
  declarationReads: number;
  /** EVERY request touching /dividends (deny-by-default proof). */
  dividendsCalls: number;
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

    if (path.startsWith("/dividends")) state.dividendsCalls += 1;

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
    if (path === "/dividends/declarations" && method === "GET") {
      await respond(200, { items: [APPROVED_DECLARATION], next_cursor: null });
      return;
    }
    if (path === `/dividends/declarations/${DECL_ID}` && method === "GET") {
      state.declarationReads += 1;
      await respond(200, APPROVED_DECLARATION);
      return;
    }
    if (path === `/dividends/declarations/${DECL_ID}/distribution` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.runBodies.push(body);
      state.runHeaders.push(await request.allHeaders());
      state.runUrls.push(request.url());
      const [status, responseBody] = state.postDistribution(body);
      await respond(status, responseBody);
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

/** Arm one distribution attempt through the typed confirmation. */
async function attemptRun(page: Page, buttonLabel: string): Promise<void> {
  await page.getByRole("button", { name: buttonLabel }).click();
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await page.getByRole("button", { name: "Run distribution", exact: true }).click();
}

test("happy path: OTP login → dividends register renders every figure VERBATIM (rates as rates, no client totals) → distribution commits exactly ONE wire POST with {batch_size: 200} ONLY and the Idempotency-Key as a HEADER", async ({
  page,
}) => {
  const state: ApiState = {
    postDistribution: () => [200, COMPLETE_RUN],
    runBodies: [],
    runHeaders: [],
    runUrls: [],
    declarationReads: 0,
    dividendsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Dividends" }).click();
  // Server decimal strings keep their trailing cents (blocker (a));
  // rates render as rates (never through the money formatter); no
  // client-computed sum exists anywhere on the page.
  await expect(page.getByText("KES 178,000.10")).toBeVisible();
  await expect(page.getByText("14.00% / 9.00%")).toBeVisible();
  await expect(page.getByText("KES 14.00")).toHaveCount(0);
  await expect(page.getByText(/Totals/)).toHaveCount(0);
  // Declarer attribution (issue #31 ledger (a).4): the SERVER's bare
  // staff UUID via the short-id convention on the register — never a
  // name or email anywhere on the page.
  await expect(page.getByTitle(DECLARER_ID)).toHaveText(DECLARER_ID.slice(0, 8));

  // Register row → detail drawer (fresh record read) → run dialog.
  await page.getByText("KES 178,000.10").click();
  await expect(page.getByText(/terminal state/)).toHaveCount(0);
  await page.getByRole("button", { name: "Run distribution…" }).click();
  await expect(page.getByText("Pinned record version")).toBeVisible();
  expect(state.declarationReads).toBeGreaterThan(0);
  // SoD arms on SERVER truth (ledger (a).4): the signed-in checker is
  // NOT the attributed declarer — the run affordance mounts with the
  // server-attribution label (the per-tab witness played no part).
  await expect(
    page.getByText(`Declaring officer ${DECLARER_ID.slice(0, 8)} (server attribution).`),
  ).toBeVisible();

  // Typed confirmation: the danger button starts DISABLED; nothing has
  // reached the wire yet.
  await page.getByRole("button", { name: "Run distribution…" }).click();
  const confirmButton = page.getByRole("button", { name: "Run distribution", exact: true });
  await expect(confirmButton).toBeDisabled();
  expect(state.runBodies).toHaveLength(0);
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await confirmButton.click();

  // The run report renders the SERVER's outcome verbatim — including
  // the issue-#19 unclaimed disposition, disclosed, never dropped…
  await expect(page.getByText("Distribution completed · declaration reconciled")).toBeVisible();
  await expect(page.getByText("Unclaimed dispositions (mid-run exits)")).toBeVisible();

  // …and exactly ONE write reached the wire: the body carries the
  // SERVER's own documented batch-size default and NOTHING else
  // (key-exact — no money parameter, no operator value can even be
  // sent), secrets ride as HEADERS, the query string is EMPTY.
  expect(state.runBodies).toHaveLength(1);
  expect(state.runBodies[0]).toEqual({ batch_size: 200 });
  expect(state.runHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.runHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.runUrls[0] ?? "").search).toBe("");
});

test("adversarial: distribution drift 409 → ONE attempt + conflict banner + explicit reload (never replays); the re-entered intent carries a ROTATED Idempotency-Key at the wire", async ({
  page,
}) => {
  let distributionStatus = 409;
  const state: ApiState = {
    postDistribution: () =>
      distributionStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-drift" }]
        : [200, COMPLETE_RUN],
    runBodies: [],
    runHeaders: [],
    runUrls: [],
    declarationReads: 0,
    dividendsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Dividends" }).click();
  await page.getByText("KES 178,000.10").click();
  await page.getByRole("button", { name: "Run distribution…" }).click();
  await expect(page.getByText("Pinned record version")).toBeVisible();

  await attemptRun(page, "Run distribution…");

  // The drift 409: conflict banner, the P13.11 remedy stated, and the
  // write was attempted exactly ONCE (no auto-retry, nothing posted).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.runBodies).toHaveLength(1);
  const staleKey = state.runHeaders[0]?.["idempotency-key"];

  // Explicit reload: the record re-fetches; NOTHING is replayed.
  const readsBeforeReload = state.declarationReads;
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByText(/void this declaration and declare afresh/)).toBeVisible();
  expect(state.declarationReads).toBeGreaterThan(readsBeforeReload);
  expect(state.runBodies).toHaveLength(1);

  // The re-entered identical intent after the acknowledged conflict is
  // a NEW intent: the key ROTATES at the wire (!60 F3) — a backend
  // idempotency store pinned to the conflicted attempt can never serve
  // the stale outcome.
  distributionStatus = 200;
  await attemptRun(page, "Run distribution…");

  await expect(page.getByText("Distribution completed · declaration reconciled")).toBeVisible();
  expect(state.runBodies).toHaveLength(2);
  expect(state.runBodies[1]).toEqual({ batch_size: 200 });
  const freshKey = state.runHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without the transactions module gets no Dividends nav entry, an Access denied guard on the direct URL and ZERO dividends calls", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: {
      role_id: "66666666-6666-6666-6666-666666666666",
      permissions: [
        {
          module: "members",
          can_view: true,
          can_create: false,
          can_edit: false,
          can_approve: false,
        },
      ],
    },
    postDistribution: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    runBodies: [],
    runHeaders: [],
    runUrls: [],
    declarationReads: 0,
    dividendsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the Dividends entry never mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Dividends" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard — and the
  // stripped role triggers ZERO /dividends requests.
  await page.goto("/modules/transactions/dividends");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Dividend declarations")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Declare dividend" })).toHaveCount(0);
  expect(state.dividendsCalls).toBe(0);
  expect(state.runBodies).toHaveLength(0);
});
