/**
 * Corrections console E2E (P15 exit criterion — issue #31 batch 1,
 * audit #30 finding R1: the P13.15 fraud channel), driven through the
 * REAL production build in a real browser — OTP login UI, session
 * custody, deny-by-default guards, generated client, the by-id
 * checker workbench, MakerCheckerPanel and the typed confirmations.
 *
 * The API is mocked at the BROWSER network boundary (page.route):
 * request counts/bodies/headers are asserted server-side-of-the-wire,
 * so the single-write proof (the approval body is the DELIBERATELY
 * EMPTY object — v1.1 rule 3), the Idempotency-Key HEADER custody and
 * the post-409 key ROTATION measure real network effects.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MAKER_ID = "88888888-8888-8888-8888-888888888888";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const REPAYMENT_ID = "33333333-4444-5555-6666-777777777777";
const ADJ_ID = "aaaaaaaa-1111-2222-3333-444444444444";

/** ConfirmDangerModal phrase for the checker writes (record id prefix). */
const PHRASE = ADJ_ID.slice(0, 8);

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

/** The pending checker snapshot. amount is DELIBERATELY not the sum of
 * its components (100.10+150.00+200.00 = 450.10): a client-side
 * re-derivation would render a figure these tests refuse to find. */
const PENDING_ADJUSTMENT = {
  id: ADJ_ID,
  repayment_id: REPAYMENT_ID,
  loan_id: LOAN_ID,
  original_transaction_id: "44444444-5555-6666-7777-888888888888",
  reversal_transaction_id: null,
  maker_id: MAKER_ID,
  checker_id: null,
  reason: "Duplicate repayment posting",
  amount: "500.00",
  penalties: "100.10",
  interest: "150.00",
  principal: "200.00",
  would_reopen_loan: false,
  status: "pending_approval",
  loan_balance_at_request: "40000.00",
  loan_penalty_due_at_request: "0.00",
  loan_status_at_request: "active",
  decided_at: null,
  version: 3,
  created_at: "2026-08-01T09:00:00+00:00",
};

const APPROVAL_RESULT = {
  adjustment_id: ADJ_ID,
  reversal_txn_id: "55555555-6666-7777-8888-999999999999",
  reversal_txn_ref: "ADJ-0001",
  amount: "500.00",
  penalties: "100.10",
  interest: "150.00",
  principal: "200.00",
  balance_after: "40500.00",
  penalty_due_after: "100.10",
  loan_status: "active",
  reopened: false,
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Returns [status, body] for the approval POST. */
  postApproval: () => [number, unknown];
  approvalBodies: string[];
  approvalHeaders: Record<string, string>[];
  approvalUrls: string[];
  adjustmentReads: number;
  /** EVERY request touching /corrections (deny-by-default proof). */
  correctionsCalls: number;
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

    if (path.startsWith("/corrections")) state.correctionsCalls += 1;

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
    // The two batch-6 registers (issue #31 ledger (a).1/(a).2): keyset
    // LIST reads the console mounts on load — served here so the
    // register legs measure real network + DOM effects.
    if (path === "/corrections/repayment-adjustments" && method === "GET") {
      await respond(200, { items: [PENDING_ADJUSTMENT], next_cursor: null });
      return;
    }
    if (path === "/corrections/write-offs" && method === "GET") {
      await respond(200, { items: [], next_cursor: null });
      return;
    }
    if (path === `/corrections/repayment-adjustments/${ADJ_ID}` && method === "GET") {
      state.adjustmentReads += 1;
      await respond(200, PENDING_ADJUSTMENT);
      return;
    }
    if (path === `/corrections/repayment-adjustments/${ADJ_ID}/approval` && method === "POST") {
      state.approvalBodies.push(request.postData() ?? "");
      state.approvalHeaders.push(await request.allHeaders());
      state.approvalUrls.push(request.url());
      const [status, responseBody] = state.postApproval();
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

/** Open the pending adjustment through the by-id checker lookup. */
async function openAdjustment(page: Page): Promise<void> {
  await page.getByLabel("Review adjustment by id").fill(ADJ_ID);
  await page.getByRole("button", { name: "Open adjustment" }).click();
  await expect(page.getByText("Record version")).toBeVisible();
}

/** Arm one approval attempt through the typed confirmation. */
async function attemptApproval(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Approve & post reversal…" }).click();
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await page.getByRole("button", { name: "Approve & post reversal", exact: true }).click();
}

test("happy path: OTP login → Corrections workbench → by-id checker review renders SERVER figures VERBATIM (no component sum) → approval commits exactly ONE wire POST with the EMPTY body and the Idempotency-Key as a HEADER", async ({
  page,
}) => {
  const state: ApiState = {
    postApproval: () => [201, APPROVAL_RESULT],
    approvalBodies: [],
    approvalHeaders: [],
    approvalUrls: [],
    adjustmentReads: 0,
    correctionsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Corrections" }).click();
  await expect(page.getByText("Repayment adjustments")).toBeVisible();
  await expect(page.getByText("Loan write-offs & recoveries")).toBeVisible();

  // The batch-6 registers render on load (issue #31 ledger (a).1/(a).2):
  // the pending adjustment sits in the checker register (server order,
  // pending-first) and the empty write-off register states itself
  // honestly — no hand-carried id is needed.
  await expect(page.getByText("Pending-adjustments checker register")).toBeVisible();
  await expect(page.getByText("Write-off committee register")).toBeVisible();
  await expect(page.getByText("No write-offs yet — nothing awaits the committee.")).toBeVisible();

  await openAdjustment(page);
  expect(state.adjustmentReads).toBeGreaterThan(0);
  // Server decimal strings keep their trailing cents (blocker (a));
  // the deliberately-broken component identity is NEVER re-derived —
  // no 450.10 anywhere on the page. ("KES 500.00" legitimately renders
  // in the register row, the detail grid AND the checker panel copy —
  // first() suffices.)
  await expect(page.getByText("KES 500.00").first()).toBeVisible();
  await expect(page.getByText("KES 100.10")).toBeVisible();
  await expect(page.getByText("KES 450.10")).toHaveCount(0);
  // Attribution under least disclosure: the maker's SHORT id renders
  // in BOTH the register row and the drawer (strict-mode: two exact
  // sites); no name or email exists for the staff UUID anywhere.
  await expect(page.getByTitle(MAKER_ID)).toHaveCount(2);
  await expect(page.getByTitle(MAKER_ID).first()).toHaveText(MAKER_ID.slice(0, 8));

  // Typed confirmation: the danger button starts DISABLED; nothing has
  // reached the wire yet.
  await page.getByRole("button", { name: "Approve & post reversal…" }).click();
  const confirmButton = page.getByRole("button", {
    name: "Approve & post reversal",
    exact: true,
  });
  await expect(confirmButton).toBeDisabled();
  expect(state.approvalBodies).toHaveLength(0);
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await confirmButton.click();

  // The result panel renders the SERVER's outcome verbatim…
  await expect(page.getByText("Reversal posted · ref ADJ-0001")).toBeVisible();
  await expect(page.getByText("KES 40,500.00")).toBeVisible();
  // …the affordance is spent…
  await expect(page.getByRole("button", { name: "Approve & post reversal…" })).toHaveCount(0);

  // …and exactly ONE write reached the wire: the body is the
  // DELIBERATELY EMPTY object (v1.1 rule 3 — the checker approves the
  // PERSISTED snapshot, supplying nothing), secrets ride as HEADERS,
  // the query string is EMPTY.
  expect(state.approvalBodies).toHaveLength(1);
  expect(JSON.parse(state.approvalBodies[0] ?? "null")).toEqual({});
  expect(state.approvalHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.approvalHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.approvalUrls[0] ?? "").search).toBe("");
});

test("adversarial: approval drift 409 (snapshot re-verification failed, NOTHING posted) → ONE attempt + conflict banner + explicit reload (never replays); the re-entered intent carries a ROTATED Idempotency-Key at the wire", async ({
  page,
}) => {
  let approvalStatus = 409;
  const state: ApiState = {
    postApproval: () =>
      approvalStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-drift" }]
        : [201, APPROVAL_RESULT],
    approvalBodies: [],
    approvalHeaders: [],
    approvalUrls: [],
    adjustmentReads: 0,
    correctionsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Corrections" }).click();
  await openAdjustment(page);

  await attemptApproval(page);

  // The drift 409: conflict banner, the P13.15 remedy stated, and the
  // write was attempted exactly ONCE (no auto-retry, nothing posted).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.approvalBodies).toHaveLength(1);
  const staleKey = state.approvalHeaders[0]?.["idempotency-key"];

  // Explicit reload: the record re-fetches; NOTHING is replayed.
  const readsBeforeReload = state.adjustmentReads;
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByText(/reject this adjustment and raise a fresh one/)).toBeVisible();
  expect(state.adjustmentReads).toBeGreaterThan(readsBeforeReload);
  expect(state.approvalBodies).toHaveLength(1);

  // The re-entered identical intent after the acknowledged conflict is
  // a NEW intent: the key ROTATES at the wire (!60 F3).
  approvalStatus = 201;
  await attemptApproval(page);

  await expect(page.getByText("Reversal posted · ref ADJ-0001")).toBeVisible();
  expect(state.approvalBodies).toHaveLength(2);
  expect(JSON.parse(state.approvalBodies[1] ?? "null")).toEqual({});
  const freshKey = state.approvalHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without the corrections module gets no Corrections nav entry, an Access denied guard on the direct URL and ZERO corrections calls", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: {
      role_id: "66666666-6666-6666-6666-666666666666",
      permissions: [
        {
          module: "transactions",
          can_view: true,
          can_create: false,
          can_edit: false,
          can_approve: false,
        },
      ],
    },
    postApproval: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    approvalBodies: [],
    approvalHeaders: [],
    approvalUrls: [],
    adjustmentReads: 0,
    correctionsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the Corrections entry never mounts.
  await expect(page.getByRole("link", { name: "Transactions" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Corrections" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard — and the
  // stripped role triggers ZERO /corrections requests.
  await page.goto("/modules/corrections");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Repayment adjustments")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Request write-off" })).toHaveCount(0);
  expect(state.correctionsCalls).toBe(0);
  expect(state.approvalBodies).toHaveLength(0);
});
