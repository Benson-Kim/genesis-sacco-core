/**
 * U2 on-screen report view E2E (P15 batch 5, audit #30 U2 — the
 * prototype's repShell/repPortfolio/repStatement "look at it now" job,
 * ZERO contract change), driven through the REAL production build in a
 * real browser.
 *
 * The API is mocked at the BROWSER network boundary (page.route): the
 * view is proven to fetch the SAME server-rendered CSV artifact the
 * download button streams (capability-token path, auth as HEADERS,
 * empty query, exactly ONE request) and to render every cell VERBATIM
 * — figures byte-identical, truncation facts surfaced verbatim,
 * hostile cells inert text. The download path stays canonical.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const EXPORT_ID = "eeeeeeee-1111-2222-3333-444444444444";
const CSV_TOKEN = "csv-Tok_en-e2e-2";
const PDF_TOKEN = "pdf-Tok_en-e2e-2";

const HOSTILE_CELL = "<img src=x onerror=window.__pwned=1>";

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

interface ArtifactShape {
  rowCount: number;
  truncated: boolean;
  rowLimit: number;
}

function exportOut(status: "queued" | "completed", artifact: ArtifactShape): Record<string, unknown> {
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
            row_count: artifact.rowCount,
            truncated: artifact.truncated,
            row_limit: artifact.rowLimit,
            expires_at: "2026-08-05T12:00:00+00:00",
            csv_download: `/exports/downloads/${CSV_TOKEN}`,
            pdf_download: `/exports/downloads/${PDF_TOKEN}`,
          }
        : null,
  };
}

interface ApiState {
  artifact: ArtifactShape;
  /** The server's OWN rendered CSV body (verbatim assertion surface). */
  csvBody: string;
  artifactRequests: { url: string; headers: Record<string, string> }[];
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
    if (path === "/exports" && method === "POST") {
      await respond(201, exportOut("queued", state.artifact));
      return;
    }
    if (path === "/jobs/exports" && method === "POST") {
      await respond(200, { completed: 1, failed: 0 });
      return;
    }
    if (path === `/exports/${EXPORT_ID}` && method === "GET") {
      await respond(200, exportOut("completed", state.artifact));
      return;
    }
    if (path === `/exports/downloads/${CSV_TOKEN}` && method === "GET") {
      state.artifactRequests.push({ url: request.url(), headers: await request.allHeaders() });
      await route.fulfill({
        status: 200,
        contentType: "text/csv; charset=utf-8",
        headers: {
          ...CORS_HEADERS,
          "access-control-expose-headers": "x-export-truncated, x-export-limit",
          "x-export-truncated": String(state.artifact.truncated),
          "x-export-limit": String(state.artifact.rowLimit),
        },
        body: state.csvBody,
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

/** Catalogue → request → drain → refresh, to a COMPLETED witnessed row. */
async function reachCompletedExport(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Reports" }).click();
  const trialCard = page.getByText("Trial balance", { exact: true }).locator("..");
  await trialCard.getByRole("button", { name: "Request export…" }).click();
  const drawer = page.getByRole("dialog", { name: "Request export" });
  await drawer.getByRole("button", { name: "Request export" }).click();
  await expect(page.getByText("Export queued · Trial balance")).toBeVisible();
  await drawer.getByRole("status").getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "Run export queue" }).click();
  const dialog = page.getByRole("dialog", { name: "Run export queue" });
  await dialog.getByRole("button", { name: "Run queue" }).click();
  await expect(page.getByText("Export queue drained")).toBeVisible();
  await dialog.getByRole("status").getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(page.getByText("Completed", { exact: true })).toBeVisible();
}

test("happy path: View renders the server's OWN rendered artifact VERBATIM — byte-identical figures, the complete-artifact fact, ONE capability-token GET with auth as HEADERS and an empty query", async ({
  page,
}) => {
  const state: ApiState = {
    artifact: { rowCount: 2, truncated: false, rowLimit: 10000 },
    csvBody: "account,debits,credits\nCash,1234.50,0.00\nLoans,250000.10,0.00\n",
    artifactRequests: [],
  };
  await mockApi(page, state);
  await login(page);
  await reachCompletedExport(page);

  // Nothing has touched the artifact route yet — the view is the
  // operator's explicit act.
  expect(state.artifactRequests).toHaveLength(0);
  await page.getByRole("button", { name: "View" }).click();

  const view = page.getByRole("dialog", { name: "Trial balance — on-screen view" });
  await expect(view).toBeVisible();

  // Every cell is the server's render, byte-identical: raw decimal
  // strings exactly as rendered server-side — no reformatting, no
  // client totals row, no re-derived figure anywhere.
  await expect(view.getByText("1234.50", { exact: true })).toBeVisible();
  await expect(view.getByText("250000.10", { exact: true })).toBeVisible();
  await expect(view.getByText("Cash", { exact: true })).toBeVisible();
  const viewText = (await view.textContent()) ?? "";
  expect(viewText).not.toContain("Total");
  // 1234.50 + 250000.10 would be 251234.60 — proven absent.
  expect(viewText).not.toContain("251234.60");

  // Truncation facts VERBATIM: server metadata + the response header.
  await expect(view.getByText(/Complete artifact: 2 rows/)).toBeVisible();
  // The canonical download affordance remains beside the view.
  await expect(page.getByRole("button", { name: "CSV" })).toBeVisible();

  // Exactly ONE artifact GET: the capability token in the PATH, auth
  // as a HEADER, an EMPTY query string — same custody as the download.
  expect(state.artifactRequests).toHaveLength(1);
  const artifactRequest = state.artifactRequests[0]!;
  expect(artifactRequest.headers["authorization"]).toMatch(/^Bearer /);
  expect(new URL(artifactRequest.url).pathname).toBe(`/exports/downloads/${CSV_TOKEN}`);
  expect(new URL(artifactRequest.url).search).toBe("");
});

test("adversarial: a TRUNCATED artifact surfaces the server's truncation facts verbatim, and hostile artifact cells render as inert TEXT — no markup materialises", async ({
  page,
}) => {
  const state: ApiState = {
    artifact: { rowCount: 2, truncated: true, rowLimit: 2 },
    csvBody: `account,debits,credits\n${HOSTILE_CELL},99.10,0.00\nCash,1.00,0.00\n`,
    artifactRequests: [],
  };
  await mockApi(page, state);
  await login(page);
  await reachCompletedExport(page);

  await page.getByRole("button", { name: "View" }).click();
  const view = page.getByRole("dialog", { name: "Trial balance — on-screen view" });
  await expect(view).toBeVisible();

  // The server's truncation facts, verbatim: row_count, row_limit and
  // the X-Export-Truncated response header value.
  await expect(
    view.getByText(/Server truncation: this artifact holds the first 2 rows/),
  ).toBeVisible();
  await expect(view.getByText(/server cap 2/)).toBeVisible();
  await expect(view.getByText(/X-Export-Truncated: true/)).toBeVisible();

  // The hostile cell is VISIBLE as text… (figures still verbatim)
  await expect(view.getByText(HOSTILE_CELL, { exact: true })).toBeVisible();
  await expect(view.getByText("99.10", { exact: true })).toBeVisible();
  // …but inert: no element materialised inside the rendered table and
  // no handler ran.
  expect(await view.locator("table img").count()).toBe(0);
  expect(await page.evaluate(() => (window as { __pwned?: unknown }).__pwned)).toBeUndefined();
});
