/**
 * #31 ledger (h1) — TWO TABS, ONE OPERATOR separation-of-duties E2E
 * (batch 8 adversarial round), through the REAL production build in
 * two ISOLATED browser contexts holding sessions for the SAME
 * principal (the per-tab write-off maker witness is deliberately
 * tab-scoped — a second tab cannot know what the first witnessed).
 *
 * Falsifiable proofs:
 * - Tab A (the requesting tab): the per-tab witness withholds the
 *   committee vote controls STRUCTURALLY — the SoD banner mounts and
 *   NO vote affordance exists (drop recordWriteOffMaker and this leg
 *   fails).
 * - Tab B (same operator, fresh tab): the witness is HONESTLY absent
 *   — the drawer states the contract does not attribute the requester
 *   and mounts the vote controls, and the SERVER refuses the
 *   self-vote (403 keyed on the persisted requested_by). Exactly ONE
 *   vote POST reaches the wire; the sanitized banner renders; nothing
 *   is retried (auto-retry would fail the count).
 *
 * The API is mocked at the BROWSER network boundary (page.route) per
 * context — vote-wire counts are asserted server-side-of-the-wire.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const WO_ID = "cccccccc-1111-2222-3333-444444444444";

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
    { module: "corrections", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

/** Requested write-off (the SoD subject). The snapshot components are
 * DELIBERATELY NON-ADDITIVE (40000.00 + 1500.10 ≠ 77777.77): a screen
 * that re-derived the total would surface a figure asserted absent by
 * the batch-1 suites; here the record simply mirrors that posture. */
const REQUESTED_WRITE_OFF = {
  id: WO_ID,
  loan_id: LOAN_ID,
  member_id: MEMBER_ID,
  balance: "40000.00",
  penalty_due: "1500.10",
  total_written_off: "77777.77",
  classification: "loss",
  provision_pct: "100.00",
  reason: "Member absconded",
  status: "requested",
  decided_at: null,
  posted_at: null,
  transaction_id: null,
  version: 2,
  created_at: "2026-08-01T09:00:00+00:00",
};

interface SodState {
  /** Vote POST bodies captured at the wire (per context). */
  voteBodies: string[];
  /** True once the create POST landed (tab A). */
  created: boolean;
}

async function mockApi(page: Page, state: SodState): Promise<void> {
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
        refresh_token: "e2e-refresh-sod",
        expires_in: 900,
      });
      return;
    }
    if (path === "/auth/refresh" && method === "POST") {
      await respond(200, {
        access_token: jwt(ADMIN_ID),
        refresh_token: "e2e-refresh-sod-rotated",
        expires_in: 900,
      });
      return;
    }
    if (path === "/me/permissions" && method === "GET") {
      await respond(200, FULL_PERMISSIONS);
      return;
    }
    if (path === "/corrections/repayment-adjustments" && method === "GET") {
      await respond(200, { items: [], next_cursor: null });
      return;
    }
    if (path === "/corrections/write-offs" && method === "GET") {
      await respond(200, {
        items: state.created ? [REQUESTED_WRITE_OFF] : [],
        next_cursor: null,
      });
      return;
    }
    if (path === "/corrections/write-offs" && method === "POST") {
      state.created = true;
      await respond(201, REQUESTED_WRITE_OFF);
      return;
    }
    if (path === `/corrections/write-offs/${WO_ID}` && method === "GET") {
      await respond(200, REQUESTED_WRITE_OFF);
      return;
    }
    if (path === `/corrections/write-offs/${WO_ID}/votes` && method === "POST") {
      state.voteBodies.push(request.postData() ?? "");
      // The SERVER verdict on the persisted requested_by: the
      // requester can never vote on their own write-off — 403,
      // sanitized category only (gate 1.6).
      await respond(403, { category: "forbidden", correlation_id: "corr-sod-403" });
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-sod-404" });
  });
}

/** Drive the REAL OTP login UI (same principal in every context). */
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

test("two tabs, one operator: the requesting tab structurally withholds the vote; a fresh tab of the SAME principal renders the honest unknown-maker state and the SERVER refuses the self-vote — exactly ONE wire attempt", async ({
  browser,
}) => {
  // ---------------- Tab A: the requesting tab ----------------
  const contextA = await browser.newContext();
  const pageA = await contextA.newPage();
  const stateA: SodState = { voteBodies: [], created: false };
  await mockApi(pageA, stateA);
  await login(pageA);

  await pageA.getByRole("link", { name: "Corrections" }).click();
  await expect(pageA.getByText("Loan write-offs & recoveries")).toBeVisible();

  // Request the write-off through the REAL drawer + typed confirmation.
  await pageA.getByRole("button", { name: "+ Request write-off" }).click();
  await pageA.getByLabel("Loan id").fill(LOAN_ID);
  await pageA.getByLabel("Reason").fill("Member absconded");
  await pageA.getByRole("button", { name: "Request write-off…" }).click();
  const phrase = LOAN_ID.slice(0, 8);
  await pageA.getByLabel(`Type "${phrase}" to confirm`).fill(phrase);
  await pageA.getByRole("button", { name: "Request write-off", exact: true }).click();
  await expect(pageA.getByText("Write-off requested · awaiting committee votes")).toBeVisible();

  // Open the record in the committee drawer FROM THE REQUESTING TAB:
  // the per-tab witness knows this operator is the maker — the SoD
  // banner mounts and NO vote control exists (structural withholding).
  await pageA.getByRole("button", { name: "Open record" }).click();
  await expect(pageA.getByText("You requested this write-off (witnessed by this tab).")).toBeVisible();
  await expect(
    pageA.getByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeVisible();
  await expect(pageA.getByRole("button", { name: "Vote approve" })).toHaveCount(0);
  await expect(pageA.getByRole("button", { name: "Vote reject" })).toHaveCount(0);
  expect(stateA.voteBodies).toHaveLength(0);

  // ---------------- Tab B: same operator, fresh tab ----------------
  const contextB = await browser.newContext();
  const pageB = await contextB.newPage();
  const stateB: SodState = { voteBodies: [], created: true };
  await mockApi(pageB, stateB);
  await login(pageB);

  await pageB.getByRole("link", { name: "Corrections" }).click();
  await pageB.getByLabel("Open write-off by id").fill(WO_ID);
  await pageB.getByRole("button", { name: "Open write-off" }).click();
  await expect(pageB.getByText("Record version")).toBeVisible();

  // The witness is HONESTLY absent in this tab: the drawer states the
  // contract gap (no requester attribution on WriteOffOut — the
  // recorded #31 follow-up) instead of inventing a maker…
  await expect(
    pageB.getByText(/not on the record .*the contract does not attribute the requester/),
  ).toBeVisible();
  // …so the vote controls mount for what the client can only treat as
  // an eligible checker. The operator votes on their own request:
  await pageB.getByRole("button", { name: "Vote approve" }).click();
  await pageB.getByLabel(`Type "${WO_ID.slice(0, 8)}" to confirm`).fill(WO_ID.slice(0, 8));
  await pageB.getByRole("button", { name: "Confirm approve vote" }).click();

  // The SERVER is the enforcer: 403 on the persisted requested_by —
  // sanitized banner, session intact, EXACTLY ONE wire attempt.
  await expect(pageB.getByText(/Not permitted\./)).toBeVisible();
  await expect(pageB.getByText(/corr-sod-403/)).toBeVisible();
  expect(stateB.voteBodies).toHaveLength(1);
  expect(JSON.parse(stateB.voteBodies[0] ?? "null")).toEqual({ vote: "approve" });
  // No auto-retry ever fires (a second body would fail this count).
  await pageB.waitForTimeout(500);
  expect(stateB.voteBodies).toHaveLength(1);

  await contextA.close();
  await contextB.close();
});
