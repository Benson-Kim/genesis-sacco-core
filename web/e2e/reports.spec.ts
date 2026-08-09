/**
 * Reports E2E (P15 exit criterion: per-module happy path + adversarial
 * flows), driven through the REAL production build in a real browser —
 * OTP login UI, session custody, deny-by-default guards, generated
 * client, export request → queue run → refresh → CSV download.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies/headers are asserted server-side-
 * of-the-wire, so the single-write double-submit proof, the post-409
 * idempotency-key rotation proof and the header-custody proof on the
 * artifact download measure real network effects, not component
 * internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const EXPORT_ID = "eeeeeeee-1111-2222-3333-444444444444";
const CSV_TOKEN = "csv-Tok_en-e2e-1";
const PDF_TOKEN = "pdf-Tok_en-e2e-1";

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
    { module: "reports", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "transactions", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function exportOut(status: "queued" | "completed"): Record<string, unknown> {
  return {
    id: EXPORT_ID,
    report: "trial_balance",
    status,
    filters: {},
    allowed_columns: ["account", "debits", "credits"],
    as_of: "2026-08-04T10:00:00+00:00",
    completed_at: status === "completed" ? "2026-08-04T10:05:00+00:00" : null,
    created_at: "2026-08-04T10:00:00+00:00",
    artifact:
      status === "completed"
        ? {
            row_count: 3,
            truncated: false,
            row_limit: 10000,
            expires_at: "2026-08-05T12:00:00+00:00",
            csv_download: `/exports/downloads/${CSV_TOKEN}`,
            pdf_download: `/exports/downloads/${PDF_TOKEN}`,
          }
        : null,
  };
}

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Returns [status, body] for POST /exports. */
  postExport: (body: Record<string, unknown>) => [number, unknown];
  exportBodies: Record<string, unknown>[];
  exportHeaders: Record<string, string>[];
  queueHeaders: Record<string, string>[];
  statusReads: number;
  downloadRequests: { url: string; headers: Record<string, string> }[];
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
    if (path === "/exports" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.exportBodies.push(body);
      state.exportHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postExport(body);
      await respond(status, responseBody);
      return;
    }
    if (path === "/jobs/exports" && method === "POST") {
      state.queueHeaders.push(await request.allHeaders());
      await respond(200, { completed: 1, failed: 0 });
      return;
    }
    if (path === `/exports/${EXPORT_ID}` && method === "GET") {
      state.statusReads += 1;
      await respond(200, exportOut("completed"));
      return;
    }
    if (path === `/exports/downloads/${CSV_TOKEN}` && method === "GET") {
      state.downloadRequests.push({ url: request.url(), headers: await request.allHeaders() });
      await route.fulfill({
        status: 200,
        contentType: "text/csv; charset=utf-8",
        headers: {
          ...CORS_HEADERS,
          "x-export-truncated": "false",
          "x-export-limit": "10000",
        },
        body: "account,debits,credits\nCash,0.00,0.00\n",
      });
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("accountant@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

test("happy path: OTP login → catalogue → ONE wire POST requests the export → queue run → refresh → CSV downloads with auth as HEADERS", async ({
  page,
}) => {
  const state: ApiState = {
    postExport: () => [201, exportOut("queued")],
    exportBodies: [],
    exportHeaders: [],
    queueHeaders: [],
    statusReads: 0,
    downloadRequests: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Reports" }).click();
  // The catalogue lists the contract's reports — the on-screen report
  // tables of the prototype are NOT reproduced (no viewing endpoint).
  await expect(page.getByText("Trial balance")).toBeVisible();
  await expect(page.getByText("Exports requested this session")).toBeVisible();

  // Request the export (no filters — the whole-tenant snapshot). The
  // catalogue card is the PARENT of its title element (the jsdom suite
  // anchors the card the same way; a div :has(title) chain resolves to
  // the deepest match — the title div itself — which holds no button).
  const trialCard = page.getByText("Trial balance", { exact: true }).locator("..");
  await trialCard.getByRole("button", { name: "Request export…" }).click();
  const drawer = page.getByRole("dialog", { name: "Request export" });
  await drawer.getByRole("button", { name: "Request export" }).click();

  // The result panel renders the SERVER's frozen request record…
  await expect(page.getByText("Export queued · Trial balance")).toBeVisible();
  // exact: the six no-filter catalogue cards' "Whole tenant — no
  // filters." scope lines also contain the (case-insensitive) phrase.
  await expect(page.getByText("whole tenant", { exact: true })).toBeVisible();
  // …and exactly ONE write reached the wire: report only (no filters
  // key at all), idempotency + bearer as HEADERS, nothing in the query.
  expect(state.exportBodies).toHaveLength(1);
  expect(state.exportBodies[0]).toEqual({ report: "trial_balance" });
  expect(state.exportHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.exportHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  // The header ✕ shares the accessible name "Close" with the result
  // panel's action — scope to the result panel (role=status).
  await drawer.getByRole("status").getByRole("button", { name: "Close" }).click();

  // The witnessed row appeared, still queued. (exact: the shared live
  // region persists "Export queued. Run the export queue to render
  // it." — a case-insensitive substring match would collide with it.)
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();

  // Drain the queue (the operations mirror of the worker loop).
  await page.getByRole("button", { name: "Run export queue" }).click();
  const dialog = page.getByRole("dialog", { name: "Run export queue" });
  await dialog.getByRole("button", { name: "Run queue" }).click();
  await expect(page.getByText("Export queue drained")).toBeVisible();
  expect(state.queueHeaders).toHaveLength(1);
  expect(state.queueHeaders[0]?.["idempotency-key"]).toBeTruthy();
  await dialog.getByRole("status").getByRole("button", { name: "Close" }).click();

  // Refresh the requester-only record: completed + server metadata.
  // (exact: the live region's "Export queue run completed." would
  // otherwise also match.)
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.getByText("Completed", { exact: true })).toBeVisible();
  expect(state.statusReads).toBeGreaterThan(0);

  // Download the CSV: a REAL browser download through the generated
  // client — bearer/tenant as HEADERS, the capability token in the
  // contract's PATH, an EMPTY query string (no secret ever in a query).
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "CSV" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("trial_balance-2026-08-04.csv");
  expect(state.downloadRequests).toHaveLength(1);
  const downloadRequest = state.downloadRequests[0]!;
  expect(downloadRequest.headers["authorization"]).toMatch(/^Bearer /);
  expect(new URL(downloadRequest.url).search).toBe("");
  expect(new URL(downloadRequest.url).pathname).toBe(`/exports/downloads/${CSV_TOKEN}`);
});

test("adversarial: a 409 on the request is ONE attempt with a sanitized banner; the acknowledged retry carries a ROTATED idempotency key at the wire", async ({
  page,
}) => {
  let exportStatus = 409;
  const state: ApiState = {
    postExport: () =>
      exportStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-conflict" }]
        : [201, exportOut("queued")],
    exportBodies: [],
    exportHeaders: [],
    queueHeaders: [],
    statusReads: 0,
    downloadRequests: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Reports" }).click();
  // The card is the title's parent element (see the happy-path note).
  const trialCard = page.getByText("Trial balance", { exact: true }).locator("..");
  await trialCard.getByRole("button", { name: "Request export…" }).click();
  const drawer = page.getByRole("dialog", { name: "Request export" });
  await drawer.getByRole("button", { name: "Request export" }).click();

  // Least-disclosure conflict: sanitized banner + operator note; the
  // request was attempted exactly once (no auto-retry, no replay).
  await expect(page.getByText(/The record changed or conflicts with existing data/)).toBeVisible();
  await expect(page.getByText(/Nothing was queued twice/)).toBeVisible();
  expect(state.exportBodies).toHaveLength(1);
  const staleKey = state.exportHeaders[0]?.["idempotency-key"];

  // The operator explicitly retries after the acknowledged conflict: a
  // NEW intent — the key ROTATES at the wire (!60 F3), so a backend
  // idempotency store pinned to the conflicted claim can never serve
  // the stale outcome.
  exportStatus = 201;
  await drawer.getByRole("button", { name: "Request export" }).click();
  await expect(page.getByText("Export queued · Trial balance")).toBeVisible();
  expect(state.exportBodies).toHaveLength(2);
  const freshKey = state.exportHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without reports:view gets no nav entry and an Access denied guard on the direct URL", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: {
      role_id: "66666666-6666-6666-6666-666666666666",
      permissions: [
        { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
      ],
    },
    postExport: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    exportBodies: [],
    exportHeaders: [],
    queueHeaders: [],
    statusReads: 0,
    downloadRequests: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the reports entry never mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Reports" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard.
  await page.goto("/modules/reports");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Exports requested this session")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run export queue" })).toHaveCount(0);
  // And the guarded screen fetched NOTHING from the exports surface.
  expect(state.exportBodies).toHaveLength(0);
  expect(state.statusReads).toBe(0);
});
