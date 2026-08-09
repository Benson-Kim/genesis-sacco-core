/**
 * U6 advisory exit-eligibility checklist E2E (P15 batch 5, audit #30
 * U6 — the prototype's vExit() criteria rows), driven through the REAL
 * production build in a real browser — OTP login UI, generated client,
 * the request-exit drawer and its typed confirmation.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): the checklist is proven to render the SERVER's facts
 * VERBATIM (counts/booleans only — no amount) BEFORE any write reaches
 * the wire; the blocked verdict is proven to withhold the submit
 * affordance with ZERO writes; the eligible path commits exactly ONE
 * key-exact POST.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXIT_ID = "eeeeeeee-aaaa-bbbb-cccc-444444444444";

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
  ],
};

const MEMBER_OUT = {
  id: MEMBER_ID,
  member_no: "M-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active",
  version: 3,
  branch_id: null,
  dividend_payout: null,
};

/** Distinct counts so each verbatim row is unambiguous on screen. */
const ELIGIBLE_FACTS = {
  member_id: MEMBER_ID,
  member_status: "active",
  status_allows_exit: true,
  open_exit_exists: false,
  live_guarantees_count: 0,
  open_applications_count: 0,
  active_loans_count: 2,
  unresolved_writeoff_claim: false,
  advisory_eligible: true,
  as_of: "2026-08-06T12:00:00+00:00",
};

const BLOCKED_FACTS = {
  ...ELIGIBLE_FACTS,
  member_status: "dormant",
  open_exit_exists: true,
  live_guarantees_count: 3,
  open_applications_count: 4,
  unresolved_writeoff_claim: true,
  advisory_eligible: false,
};

/** The server's snapshot for the created request (result panel). */
const CREATED_EXIT = {
  id: EXIT_ID,
  member_id: MEMBER_ID,
  status: "requested",
  reason: null,
  requested_by: null,
  shares_amount: "25000.10",
  deposits_amount: "150000.10",
  loan_balance: "40000.00",
  fees: "500.00",
  net_payable: "134500.20",
  decided_at: null,
  settled_at: null,
  settlement_txn_id: null,
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

interface ApiState {
  /** Facts served to the eligibility read. */
  facts: unknown;
  eligibilityCalls: number;
  createBodies: Record<string, unknown>[];
  createHeaders: Record<string, string>[];
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
    if (path === `/member-exits/eligibility/${MEMBER_ID}` && method === "GET") {
      state.eligibilityCalls += 1;
      await respond(200, state.facts);
      return;
    }
    if (path === "/member-exits" && method === "GET") {
      await respond(200, { items: [], next_cursor: null });
      return;
    }
    if (path === "/member-exits" && method === "POST") {
      state.createBodies.push(request.postDataJSON() as Record<string, unknown>);
      state.createHeaders.push(await request.allHeaders());
      await respond(201, CREATED_EXIT);
      return;
    }
    if (path === `/members/${MEMBER_ID}` && method === "GET") {
      await respond(200, MEMBER_OUT);
      return;
    }
    if (path === "/members" && method === "GET") {
      await respond(200, { items: [MEMBER_OUT], next_cursor: null });
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

/** Open the request drawer and choose the (only) member. */
async function openDrawerAndPick(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Member exit" }).click();
  await page.getByRole("button", { name: "+ Request exit" }).click();
  // exact: the drawer's own dialog label ("Request member exit")
  // substring-matches a bare "Member" under strict mode.
  await page.getByLabel("Member", { exact: true }).selectOption(MEMBER_ID);
}

test("happy path: the checklist renders the server's facts VERBATIM (counts/booleans, NO amount) BEFORE submission; the request then commits exactly ONE key-exact POST", async ({
  page,
}) => {
  const state: ApiState = {
    facts: ELIGIBLE_FACTS,
    eligibilityCalls: 0,
    createBodies: [],
    createHeaders: [],
  };
  await mockApi(page, state);
  await login(page);
  await openDrawerAndPick(page);

  // The checklist landed from the ADVISORY read, BEFORE any write.
  const checklist = page.getByTestId("eligibility-checklist");
  await expect(checklist).toBeVisible();
  expect(state.eligibilityCalls).toBeGreaterThan(0);
  expect(state.createBodies).toHaveLength(0);

  // Verbatim rows: the informational active-loan count, the verdict,
  // the honest lock-free caveat — and NO amount anywhere in it.
  await expect(checklist).toContainText("Advisory eligibility checklist");
  await expect(checklist).toContainText("eligible to request");
  await expect(checklist).toContainText("re-verdicts every blocker under locks");
  const checklistText = (await checklist.textContent()) ?? "";
  expect(checklistText).not.toContain("KES");
  expect(checklistText).not.toMatch(/\d+\.\d{2}/);

  // Submit through the typed confirmation (the member number).
  await page.getByRole("button", { name: "Request exit…" }).click();
  await page.getByLabel('Type "M-0001" to confirm').fill("M-0001");
  await page.getByRole("button", { name: "Request exit", exact: true }).click();

  // The SERVER's snapshot renders in the result panel…
  await expect(page.getByText("Exit requested · awaiting committee decision")).toBeVisible();
  // …and exactly ONE wire POST carried the key-exact body: {member_id,
  // reason} and NOTHING else — no money field can even be sent; the
  // Idempotency-Key rode as a HEADER.
  expect(state.createBodies).toHaveLength(1);
  expect(state.createBodies[0]).toEqual({ member_id: MEMBER_ID, reason: null });
  expect(Object.keys(state.createBodies[0] ?? {}).sort()).toEqual(["member_id", "reason"]);
  expect(state.createHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.createHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("adversarial: a BLOCKED advisory verdict renders the blocker rows verbatim and withholds the submit affordance — ZERO writes reach the wire", async ({
  page,
}) => {
  const state: ApiState = {
    facts: BLOCKED_FACTS,
    eligibilityCalls: 0,
    createBodies: [],
    createHeaders: [],
  };
  await mockApi(page, state);
  await login(page);
  await openDrawerAndPick(page);

  const checklist = page.getByTestId("eligibility-checklist");
  await expect(checklist).toBeVisible();
  await expect(checklist).toContainText("one already exists — BLOCKS");
  await expect(checklist).toContainText("3");
  await expect(checklist).toContainText("4");
  await expect(checklist).toContainText("yes — BLOCKS");
  await expect(checklist).toContainText("blocked");

  // SELF-GATING with the honest server-enforces-regardless banner.
  await expect(page.getByRole("button", { name: "Request exit…" })).toBeDisabled();
  await expect(page.getByText("Advisory blockers are present")).toBeVisible();
  expect(state.createBodies).toHaveLength(0);
});
