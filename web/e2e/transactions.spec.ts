/**
 * Transactions E2E (P15 exit criterion: per-module happy path +
 * adversarial flows), driven through the REAL production build in a
 * real browser — OTP login UI, session custody, deny-by-default
 * guards, generated client, typed-confirmation teller posting.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies/headers are asserted server-side-
 * of-the-wire, so the single-write double-post proof and the post-409
 * idempotency-key rotation proof measure real network effects, not
 * component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const DEBIT_TXN_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const CREDIT_TXN_ID = "bbbbbbbb-1111-2222-3333-444444444444";

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
    { module: "transactions", can_view: true, can_create: true, can_edit: true, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

const DEBIT_TXN = {
  id: DEBIT_TXN_ID,
  txn_ref: "WD-0221",
  member_id: MEMBER_ID,
  type: "withdrawal",
  amount: "8000.10",
  channel: "bank",
  direction: "debit",
  occurred_at: "2026-07-18T10:15:00+00:00",
  is_reversal: false,
  created_by: null,
  external_ref: null,
};

const CREDIT_TXN = {
  id: CREDIT_TXN_ID,
  txn_ref: "MP-1042",
  member_id: MEMBER_ID,
  type: "deposit",
  amount: "5000.10",
  channel: "mpesa",
  direction: "credit",
  occurred_at: "2026-07-19T08:00:00+00:00",
  is_reversal: false,
  created_by: null,
  external_ref: "SGH3KLM9QT",
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

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** Returns [status, body] for the teller POSTs. */
  postMoney: (body: Record<string, unknown>) => [number, unknown];
  postBodies: Record<string, unknown>[];
  postHeaders: Record<string, string>[];
  postPaths: string[];
  memberReads: number;
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
    if (path === "/transactions" && method === "GET") {
      await respond(200, { items: [DEBIT_TXN, CREDIT_TXN], next_cursor: null });
      return;
    }
    if (path === "/members" && method === "GET") {
      await respond(200, { items: [MEMBER_OUT], next_cursor: null });
      return;
    }
    if (path === `/members/${MEMBER_ID}` && method === "GET") {
      state.memberReads += 1;
      await respond(200, MEMBER_OUT);
      return;
    }
    if (
      (path === `/members/${MEMBER_ID}/deposits` ||
        path === `/members/${MEMBER_ID}/withdrawals` ||
        path === `/members/${MEMBER_ID}/share-topups`) &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.postBodies.push(body);
      state.postHeaders.push(await request.allHeaders());
      state.postPaths.push(path);
      const [status, responseBody] = state.postMoney(body);
      await respond(status, responseBody);
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("teller@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

test("happy path (money write): OTP login → ledger renders DR/CR VERBATIM → deposit commits exactly ONE POST through the typed confirmation", async ({
  page,
}) => {
  const state: ApiState = {
    postMoney: () => [
      201,
      {
        txn_id: "eeeeeeee-1111-2222-3333-444444444444",
        txn_ref: "MP-1099",
        amount: "5000.10",
        balance_after: "173500.10",
      },
    ],
    postBodies: [],
    postHeaders: [],
    postPaths: [],
    memberReads: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Transactions" }).click();
  // Server decimal strings keep their trailing cents in the DR and CR
  // columns (blocker (a)) — and no locally computed totals row exists.
  await expect(page.getByText("KES 8,000.10")).toBeVisible();
  await expect(page.getByText("KES 5,000.10")).toBeVisible();
  // No locally summed/netted figure exists anywhere on the page.
  await expect(page.getByText("KES 13,000.20")).toHaveCount(0);
  await expect(page.getByText("KES 3,000.00")).toHaveCount(0);

  await page.getByRole("button", { name: "+ Post transaction" }).click();
  const drawer = page.getByRole("dialog", { name: "Post transaction" });
  // #35 item 14: unique-number blur lookup — no member-list select.
  await drawer.getByLabel("Member number").fill("M-0001");
  await drawer.getByLabel("Member number").blur();
  // The FRESH member read (staleTime 0) lands before the confirmation
  // can arm (stale-read prevention, pattern (e)).
  await expect(drawer.getByText(/Member verified: Jane Wanjiku · M-0001/)).toBeVisible();
  expect(state.memberReads).toBeGreaterThan(0);
  await drawer.getByLabel("Amount (KES)").fill("5000.10");
  await drawer.getByLabel("Channel").selectOption("mpesa");
  await drawer.getByLabel("External reference").fill("SGH3KLM9QT");
  await drawer.getByRole("button", { name: "Post to ledger…" }).click();

  // Typed confirmation: the write happens only after the byte-identical
  // phrase; the confirm button starts disabled.
  const confirm = page.getByRole("dialog", { name: "Post to ledger" });
  const confirmButton = confirm.getByRole("button", { name: "Post to ledger" });
  await expect(confirmButton).toBeDisabled();
  expect(state.postBodies).toHaveLength(0);
  await confirm.getByLabel('Type "M-0001" to confirm').fill("M-0001");
  await confirmButton.click();

  // The result panel renders the SERVER's figures verbatim…
  await expect(page.getByText("Deposit posted · ref MP-1099")).toBeVisible();
  await expect(page.getByText("KES 173,500.10")).toBeVisible();
  // …the affordance is spent…
  await expect(drawer.getByRole("button", { name: "Post to ledger…" })).toHaveCount(0);

  // …and exactly ONE write reached the wire: the deposit route, the
  // typed decimal STRING byte-identical, idempotency + bearer as
  // HEADERS, nothing in the query string.
  expect(state.postBodies).toHaveLength(1);
  expect(state.postPaths[0]).toBe(`/members/${MEMBER_ID}/deposits`);
  expect(state.postBodies[0]).toEqual({
    amount: "5000.10",
    channel: "mpesa",
    external_ref: "SGH3KLM9QT",
  });
  expect(state.postHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.postHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("adversarial (money write): stale withdrawal → 409 conflict banner, EXACTLY ONE write, explicit reload never replays; the re-entered intent carries a ROTATED idempotency key at the wire", async ({
  page,
}) => {
  let withdrawalStatus = 409;
  const state: ApiState = {
    postMoney: () =>
      withdrawalStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-stale" }]
        : [
            201,
            {
              txn_id: "ffffffff-1111-2222-3333-444444444444",
              txn_ref: "WD-0300",
              amount: "8000.00",
              balance_after: "165500.10",
            },
          ],
    postBodies: [],
    postHeaders: [],
    postPaths: [],
    memberReads: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Transactions" }).click();
  await page.getByRole("button", { name: "+ Post transaction" }).click();
  const drawer = page.getByRole("dialog", { name: "Post transaction" });
  await drawer.getByLabel("Type").selectOption("withdrawal");
  await drawer.getByLabel("Member number").fill("M-0001");
  await drawer.getByLabel("Member number").blur();
  await expect(drawer.getByText(/Member verified:/)).toBeVisible();
  await drawer.getByLabel("Amount (KES)").fill("8000.00");
  await drawer.getByLabel("Channel").selectOption("bank");
  await drawer.getByLabel("External reference").fill("SLIP-4471");
  await drawer.getByRole("button", { name: "Post to ledger…" }).click();
  const confirm = page.getByRole("dialog", { name: "Post to ledger" });
  await confirm.getByLabel('Type "M-0001" to confirm').fill("M-0001");
  await confirm.getByRole("button", { name: "Post to ledger" }).click();

  // The conflict banner offers the explicit reload flow; the write was
  // NOT applied and was attempted exactly once (no auto-retry).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.postBodies).toHaveLength(1);
  const staleKey = state.postHeaders[0]?.["idempotency-key"];

  // Reload re-fetches the member; the conflicted posting is withdrawn
  // and NOTHING was replayed.
  const readsBeforeReload = state.memberReads;
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByText(/conflicted posting was withdrawn, nothing was replayed/)).toBeVisible();
  expect(state.memberReads).toBeGreaterThan(readsBeforeReload);
  expect(state.postBodies).toHaveLength(1);

  // Re-entering the IDENTICAL entry after the reload is a NEW intent:
  // the key ROTATES at the wire (!60 F3 — a backend idempotency store
  // pinned to the conflicted attempt can never serve the stale outcome).
  withdrawalStatus = 201;
  await drawer.getByRole("button", { name: "Post to ledger…" }).click();
  const confirm2 = page.getByRole("dialog", { name: "Post to ledger" });
  await confirm2.getByLabel('Type "M-0001" to confirm').fill("M-0001");
  await confirm2.getByRole("button", { name: "Post to ledger" }).click();

  await expect(page.getByText("Withdrawal posted · ref WD-0300")).toBeVisible();
  expect(state.postBodies).toHaveLength(2);
  expect(state.postPaths[1]).toBe(`/members/${MEMBER_ID}/withdrawals`);
  const freshKey = state.postHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without transactions:view gets no nav entry and an Access denied guard on the direct URL", async ({
  page,
}) => {
  const state: ApiState = {
    permissions: {
      role_id: "66666666-6666-6666-6666-666666666666",
      permissions: [
        { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
      ],
    },
    postMoney: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    postBodies: [],
    postHeaders: [],
    postPaths: [],
    memberReads: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the transactions entry never mounts.
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Transactions" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard.
  await page.goto("/modules/transactions");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Transactions ledger")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Post transaction" })).toHaveCount(0);
});
