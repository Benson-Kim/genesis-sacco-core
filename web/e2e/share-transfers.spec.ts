/**
 * Share-transfers console E2E (issue #31 batch 10, ledger (l)/(m) —
 * the !77 human-authorized maker-checker rework of the batch-6 (e)
 * console), driven through the REAL production build in a real
 * browser — OTP login UI, session custody, deny-by-default guards,
 * generated client, MakerCheckerPanel, the typed confirmations and
 * the Idempotency-Key custody.
 *
 * The API is mocked at the BROWSER network boundary (page.route):
 * request counts/bodies/headers are asserted server-side-of-the-wire,
 * so the single-write proofs (the MAKER body is EXACTLY
 * {to_member_id, amount} with the typed decimal STRING verbatim; the
 * CHECKER approval body is the DELIBERATELY EMPTY object — v1.1 rule
 * 3), the header custody and the deny-by-default zero-call proof
 * measure real network effects.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MAKER_ID = "88888888-8888-8888-8888-888888888888";
const FROM_ID = "11111111-1111-1111-1111-111111111111";
const TO_ID = "33333333-3333-3333-3333-333333333333";
const PENDING_ID = "aaaabbbb-1111-2222-3333-444444444444";
const POSTED_ID = "ccccdddd-1111-2222-3333-444444444444";

/** ConfirmDangerModal phrases: the MAKER types the transferring
 * member's id prefix; the CHECKER types the transfer record's. */
const MAKER_PHRASE = FROM_ID.slice(0, 8);
const CHECKER_PHRASE = PENDING_ID.slice(0, 8);

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

/** ShareTransferRecordOut — the MAKER's request response AND the
 * register's pending row. created_by is a DIFFERENT officer, so the
 * signed-in admin is an eligible checker in the drawer tests. */
const PENDING_RECORD = {
  id: PENDING_ID,
  from_member_id: FROM_ID,
  to_member_id: TO_ID,
  amount: "5000.10",
  status: "pending",
  out_transaction_id: null,
  in_transaction_id: null,
  created_by: MAKER_ID,
  approved_by: null,
  decided_at: null,
  version: 1,
  created_at: "2026-08-07T09:00:00Z",
};

/** Terminal history row served BEHIND the pending band — the server's
 * pending-first register order, rendered verbatim. */
const POSTED_RECORD = {
  id: POSTED_ID,
  from_member_id: FROM_ID,
  to_member_id: TO_ID,
  amount: "120.55",
  status: "posted",
  out_transaction_id: "eeeeffff-1111-2222-3333-444444444444",
  in_transaction_id: "eeeeffff-5555-6666-7777-888888888888",
  created_by: MAKER_ID,
  approved_by: ADMIN_ID,
  decided_at: "2026-08-06T12:00:00Z",
  version: 2,
  created_at: "2026-08-06T11:00:00Z",
};

/**
 * NON-ADDITIVE approval response (blocker (a)): no rendered figure
 * equals the sum of any two others — a client-side sum cannot pass as
 * a coincidence; the sums are asserted ABSENT below (5000.10 +
 * 15000.25 = 20005.35; 5000.10 + 20000.35 = 25005.45; 15000.25 +
 * 20000.35 = 35000.60).
 */
const APPROVE_RESULT = {
  transfer_id: PENDING_ID,
  out_txn_ref: "ST-OUT-000123",
  in_txn_ref: "ST-IN-000123",
  amount: "5000.10",
  from_balance_after: "15000.25",
  to_balance_after: "20000.35",
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** The record served to the drawer's fresh by-id read. */
  detailRecord?: unknown;
  /** Returns [status, body] for the MAKER request POST. */
  postRequest: () => [number, unknown];
  /** Returns [status, body] for the CHECKER approval POST. */
  postApproval: () => [number, unknown];
  /** Returns [status, body] for the CHECKER rejection POST. */
  postRejection: () => [number, unknown];
  requestBodies: string[];
  requestHeaders: Record<string, string>[];
  requestUrls: string[];
  approvalBodies: string[];
  approvalHeaders: Record<string, string>[];
  approvalUrls: string[];
  rejectionBodies: string[];
  rejectionHeaders: Record<string, string>[];
  detailReads: number;
  /** EVERY request touching share-transfers (deny-by-default proof). */
  transferCalls: number;
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

    if (path.includes("/share-transfers")) state.transferCalls += 1;

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
    // The ledger-(m) register (members:view): the server's
    // PENDING-FIRST order — the console renders it verbatim.
    if (path === "/share-transfers" && method === "GET") {
      await respond(200, { items: [PENDING_RECORD, POSTED_RECORD], next_cursor: null });
      return;
    }
    // The drawer's FRESH by-id read behind every checker write.
    if (path === `/share-transfers/${PENDING_ID}` && method === "GET") {
      state.detailReads += 1;
      await respond(200, state.detailRecord ?? PENDING_RECORD);
      return;
    }
    // MAKER phase: creates the PENDING workflow record; NO money moves.
    if (path === `/members/${FROM_ID}/share-transfers` && method === "POST") {
      state.requestBodies.push(request.postData() ?? "");
      state.requestHeaders.push(await request.allHeaders());
      state.requestUrls.push(request.url());
      const [status, responseBody] = state.postRequest();
      await respond(status, responseBody);
      return;
    }
    // CHECKER phase: approval (EMPTY body by contract).
    if (path === `/share-transfers/${PENDING_ID}/approval` && method === "POST") {
      state.approvalBodies.push(request.postData() ?? "");
      state.approvalHeaders.push(await request.allHeaders());
      state.approvalUrls.push(request.url());
      const [status, responseBody] = state.postApproval();
      await respond(status, responseBody);
      return;
    }
    // CHECKER phase: version-pinned rejection with MANDATORY rationale.
    if (path === `/share-transfers/${PENDING_ID}/rejection` && method === "POST") {
      state.rejectionBodies.push(request.postData() ?? "");
      state.rejectionHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postRejection();
      await respond(status, responseBody);
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

function freshState(): ApiState {
  return {
    postRequest: () => [201, PENDING_RECORD],
    postApproval: () => [201, APPROVE_RESULT],
    postRejection: () => [
      200,
      {
        ...PENDING_RECORD,
        status: "rejected",
        approved_by: ADMIN_ID,
        decided_at: "2026-08-07T10:00:00Z",
        version: 2,
      },
    ],
    requestBodies: [],
    requestHeaders: [],
    requestUrls: [],
    approvalBodies: [],
    approvalHeaders: [],
    approvalUrls: [],
    rejectionBodies: [],
    rejectionHeaders: [],
    detailReads: 0,
    transferCalls: 0,
  };
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

/** Open the pending transfer's review drawer from the register. */
async function openPendingRow(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Share transfers" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(2);
  await page.locator("tbody tr").first().click();
  await expect(page.getByText("Record version")).toBeVisible();
}

test("MAKER phase: OTP login → register renders the server's PENDING-FIRST order verbatim → typed confirm starts DISARMED → exactly ONE wire POST with {to_member_id, amount} ONLY (typed decimal STRING) and the Idempotency-Key as a HEADER → the PENDING panel states NO money moved", async ({
  page,
}) => {
  const state = freshState();
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Share transfers" }).click();

  // The ledger-(m) register renders the SERVER's pending-first order
  // verbatim — never re-sorted locally.
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("Pending approval");
  await expect(rows.nth(0)).toContainText(PENDING_ID.slice(0, 8));
  await expect(rows.nth(1)).toContainText("Posted");
  await expect(rows.nth(1)).toContainText(POSTED_ID.slice(0, 8));
  // Server decimal strings verbatim in the rows (blocker (a)).
  await expect(rows.nth(1)).toContainText("KES 120.55");

  await page.getByLabel("Transferring member id").fill(FROM_ID);
  await page.getByLabel("Receiving member id").fill(TO_ID);
  await page.getByLabel("Amount (KES)").fill("5000.10");
  await page.getByRole("button", { name: "Request transfer…" }).click();

  // Typed confirmation: the danger button starts DISABLED; nothing
  // has reached the wire yet.
  const confirmButton = page.getByRole("button", { name: "Request transfer", exact: true });
  await expect(confirmButton).toBeDisabled();
  expect(state.requestBodies).toHaveLength(0);
  await page.getByLabel(`Type "${MAKER_PHRASE}" to confirm`).fill(MAKER_PHRASE);
  await confirmButton.click();

  // The SPENT affordance: the PENDING panel replaces the form and
  // states that NO money moved — the request response carries no
  // balance figure to render (the snapshot lives server-side).
  await expect(page.getByText("Transfer request recorded · PENDING approval")).toBeVisible();
  await expect(page.getByText(/NO money moved/)).toBeVisible();
  await expect(page.getByLabel("Transferring member id")).toHaveCount(0);

  // Exactly ONE write reached the wire: the transferring member rides
  // the PATH, the body is KEY-EXACT {to_member_id, amount} with the
  // typed decimal STRING verbatim, secrets ride as HEADERS, the query
  // string is EMPTY.
  expect(state.requestBodies).toHaveLength(1);
  expect(JSON.parse(state.requestBodies[0] ?? "null")).toEqual({
    to_member_id: TO_ID,
    amount: "5000.10",
  });
  expect(state.requestHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.requestHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.requestUrls[0] ?? "").search).toBe("");
  expect(state.approvalBodies).toHaveLength(0);
  expect(state.rejectionBodies).toHaveLength(0);
});

test("CHECKER phase: a register row opens the review drawer (fresh by-id read) → typed-confirm approval commits exactly ONE wire POST with the DELIBERATELY EMPTY body {} and the Idempotency-Key as a HEADER → the NON-ADDITIVE result renders VERBATIM with every sum ABSENT", async ({
  page,
}) => {
  const state = freshState();
  await mockApi(page, state);
  await login(page);

  await openPendingRow(page);
  expect(state.detailReads).toBeGreaterThan(0);

  // The maker is a DIFFERENT officer — the checker affordances mount
  // (MakerCheckerPanel; the maker identity is the CONTRACT's
  // created_by, server truth).
  await expect(page.getByText(/Requesting officer 88888888/)).toBeVisible();

  await page.getByRole("button", { name: "Approve & post transfer…" }).click();
  const confirmButton = page.getByRole("button", {
    name: "Approve & post transfer",
    exact: true,
  });
  await expect(confirmButton).toBeDisabled();
  expect(state.approvalBodies).toHaveLength(0);
  await page.getByLabel(`Type "${CHECKER_PHRASE}" to confirm`).fill(CHECKER_PHRASE);
  await confirmButton.click();

  // The result panel renders the SERVER's figures VERBATIM (trailing
  // cents survive) — and NO sum of any two figures exists anywhere
  // (5000.10 + 15000.25 = 20005.35; + 20000.35 = 25005.45; 15000.25 +
  // 20000.35 = 35000.60): the two balances belong to two different
  // members and are never summed, netted or reconciled client-side.
  await expect(page.getByText("Transfer posted · ST-OUT-000123 / ST-IN-000123")).toBeVisible();
  await expect(page.getByText("KES 5,000.10").first()).toBeVisible();
  await expect(page.getByText("KES 15,000.25")).toBeVisible();
  await expect(page.getByText("KES 20,000.35")).toBeVisible();
  await expect(page.getByText("KES 20,005.35")).toHaveCount(0);
  await expect(page.getByText("KES 25,005.45")).toHaveCount(0);
  await expect(page.getByText("KES 35,000.60")).toHaveCount(0);
  // The SPENT affordance: the checker controls are gone.
  await expect(page.getByRole("button", { name: "Approve & post transfer…" })).toHaveCount(0);

  // Exactly ONE write reached the wire: the body is the DELIBERATELY
  // EMPTY object (v1.1 rule 3 — the checker approves the PERSISTED
  // snapshot, supplying nothing), secrets ride as HEADERS, the query
  // string is EMPTY.
  expect(state.approvalBodies).toHaveLength(1);
  expect(JSON.parse(state.approvalBodies[0] ?? "null")).toEqual({});
  expect(state.approvalHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.approvalHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.approvalUrls[0] ?? "").search).toBe("");
});

test("adversarial: STRUCTURAL SoD withhold (blocker (f)) — when the signed-in user IS the maker (server-truth created_by), the drawer NEVER mounts approve/reject controls; the SoD banner renders and ZERO decision calls reach the wire", async ({
  page,
}) => {
  const state = freshState();
  state.detailRecord = { ...PENDING_RECORD, created_by: ADMIN_ID };
  await mockApi(page, state);
  await login(page);

  await openPendingRow(page);

  await expect(page.getByText(/You initiated this action/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve & post transfer…" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reject transfer…" })).toHaveCount(0);
  expect(state.approvalBodies).toHaveLength(0);
  expect(state.rejectionBodies).toHaveLength(0);
});

test("adversarial: rejection REQUIRES the rationale (!52 F2) — an empty reason never arms the confirm (zero calls); a valid reason pins EXACTLY {version, reason} on the wire through the typed confirmation", async ({
  page,
}) => {
  const state = freshState();
  await mockApi(page, state);
  await login(page);

  await openPendingRow(page);
  await expect(page.getByText(/Requesting officer 88888888/)).toBeVisible();

  // Empty rationale: refused BEFORE any confirmation arms — nothing
  // reaches the wire.
  await page.getByRole("button", { name: "Reject transfer…" }).click();
  await expect(
    page.getByText("The rejection rationale is required (four-eyes practice)."),
  ).toBeVisible();
  expect(state.rejectionBodies).toHaveLength(0);

  await page
    .getByLabel("Rejection rationale (required to reject)")
    .fill("Amount keyed against the wrong member.");
  await page.getByRole("button", { name: "Reject transfer…" }).click();
  const confirmButton = page.getByRole("button", { name: "Reject transfer", exact: true });
  await expect(confirmButton).toBeDisabled();
  expect(state.rejectionBodies).toHaveLength(0);
  await page.getByLabel(`Type "${CHECKER_PHRASE}" to confirm`).fill(CHECKER_PHRASE);
  await confirmButton.click();

  // Exactly ONE write: the body pins the FRESH record version AND the
  // mandatory rationale — nothing else rides the wire; no money moved
  // (the approval endpoint was never touched).
  await expect
    .poll(() => state.rejectionBodies.length, { message: "one rejection POST" })
    .toBe(1);
  expect(JSON.parse(state.rejectionBodies[0] ?? "null")).toEqual({
    version: 1,
    reason: "Amount keyed against the wrong member.",
  });
  expect(state.rejectionHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.approvalBodies).toHaveLength(0);
});

test("adversarial: deny-by-default — a role without the members module gets no Share transfers nav entry, an Access denied guard on the direct URL and ZERO share-transfer calls of ANY kind", async ({
  page,
}) => {
  const state = freshState();
  state.permissions = {
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
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the Share transfers entry never mounts.
  await expect(page.getByRole("link", { name: "Transactions" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Share transfers" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard — ZERO
  // share-transfer requests from the stripped role (reads included).
  await page.goto("/modules/members/share-transfers");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByLabel("Transferring member id")).toHaveCount(0);
  expect(state.transferCalls).toBe(0);
  expect(state.requestBodies).toHaveLength(0);
  expect(state.approvalBodies).toHaveLength(0);
  expect(state.rejectionBodies).toHaveLength(0);
});

test("adversarial: members:view-only reaches the console but the request form is structurally WITHHELD (equity moves are approve-gated) — the (m) register STILL renders (the read surface); zero write calls possible", async ({
  page,
}) => {
  const state = freshState();
  state.permissions = {
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
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Share transfers" }).click();
  await expect(page.getByText(/no members approval permission/)).toBeVisible();
  await expect(page.getByLabel("Transferring member id")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Request transfer…" })).toHaveCount(0);

  // The register is members:view — the (m) read surface renders for
  // this role, pending-first, server figures verbatim.
  const rows = page.locator("tbody tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("Pending approval");
  await expect(rows.nth(1)).toContainText("Posted");

  expect(state.requestBodies).toHaveLength(0);
  expect(state.approvalBodies).toHaveLength(0);
  expect(state.rejectionBodies).toHaveLength(0);
});
