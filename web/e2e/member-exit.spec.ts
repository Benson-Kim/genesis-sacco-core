/**
 * Member-exit E2E (P15 exit criterion: per-module happy path +
 * adversarial flows; module 7 — the P12 exit & settlement API), driven
 * through the REAL production build in a real browser — OTP login UI,
 * session custody, deny-by-default guards, generated client, the
 * maker-checker settlement dialog and its typed confirmation.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin): request counts/bodies/headers are asserted server-side-
 * of-the-wire, so the single-write proof (body carries {version,
 * channel} ONLY — key-exact), the Idempotency-Key HEADER custody and
 * the post-409 key ROTATION proof measure real network effects, not
 * component internals.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXIT_ID = "eeeeeeee-aaaa-bbbb-cccc-444444444444";
const NEGATIVE_EXIT_ID = "ffffffff-aaaa-bbbb-cccc-444444444444";

/** ConfirmDangerModal phrase for the settlement (record id prefix). */
const PHRASE = EXIT_ID.slice(0, 8);

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

/** The committee-APPROVED settlement snapshot awaiting posting. */
const APPROVED_EXIT = {
  id: EXIT_ID,
  member_id: MEMBER_ID,
  status: "approved",
  reason: "Relocation",
  requested_by: null,
  shares_amount: "25000.10",
  deposits_amount: "150000.10",
  loan_balance: "40000.00",
  fees: "500.00",
  net_payable: "134500.20",
  decided_at: "2026-08-02T10:00:00+00:00",
  settled_at: null,
  settlement_txn_id: null,
  version: 7,
  created_at: "2026-08-01T09:00:00+00:00",
};

/** A second row on the documented NEGATIVE branch (sign verbatim). */
const NEGATIVE_EXIT = {
  ...APPROVED_EXIT,
  id: NEGATIVE_EXIT_ID,
  status: "requested",
  decided_at: null,
  net_payable: "-4000.00",
};

const SETTLED_RESULT = {
  exit: {
    ...APPROVED_EXIT,
    status: "settled",
    settled_at: "2026-08-03T10:00:00+00:00",
    settlement_txn_id: "dddddddd-1111-2222-3333-444444444444",
    version: 8,
  },
  txn_ref: "EX-0001",
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
  /** Returns [status, body] for the settlement POST. */
  postSettlement: (body: Record<string, unknown>) => [number, unknown];
  settleBodies: Record<string, unknown>[];
  settleHeaders: Record<string, string>[];
  settleUrls: string[];
  exitReads: number;
  /** EVERY request touching /member-exits (deny-by-default proof). */
  exitsCalls: number;
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

    if (path.startsWith("/member-exits")) state.exitsCalls += 1;

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
    if (path === "/member-exits" && method === "GET") {
      await respond(200, { items: [APPROVED_EXIT, NEGATIVE_EXIT], next_cursor: null });
      return;
    }
    if (path === `/member-exits/${EXIT_ID}` && method === "GET") {
      state.exitReads += 1;
      await respond(200, APPROVED_EXIT);
      return;
    }
    if (path === `/member-exits/${EXIT_ID}/settlement` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.settleBodies.push(body);
      state.settleHeaders.push(await request.allHeaders());
      state.settleUrls.push(request.url());
      const [status, responseBody] = state.postSettlement(body);
      await respond(status, responseBody);
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

/** Arm one settlement attempt through the typed confirmation. */
async function attemptSettlement(page: Page): Promise<void> {
  await page.getByLabel("Payout channel").selectOption("mpesa");
  await page.getByRole("button", { name: "Post settlement…" }).click();
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await page.getByRole("button", { name: "Post settlement", exact: true }).click();
}

test("happy path: OTP login → exit register renders figures VERBATIM (negative sign intact, no client totals) → settlement commits exactly ONE wire POST with {version, channel} ONLY and the Idempotency-Key as a HEADER", async ({
  page,
}) => {
  const state: ApiState = {
    postSettlement: () => [201, SETTLED_RESULT],
    settleBodies: [],
    settleHeaders: [],
    settleUrls: [],
    exitReads: 0,
    exitsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Member exit" }).click();
  // Server decimal strings keep their trailing cents (blocker (a)); a
  // NEGATIVE net payable renders verbatim WITH its sign; no client-
  // computed sum exists anywhere on the page.
  await expect(page.getByText("KES 134,500.20")).toBeVisible();
  await expect(page.getByText("KES -4,000.00")).toBeVisible();
  await expect(page.getByText("KES 175,000.20")).toHaveCount(0);
  await expect(page.getByText(/Totals/)).toHaveCount(0);

  // Register row → detail drawer (fresh record read) → settle dialog.
  await page.getByText("KES 134,500.20").click();
  await expect(page.getByText(/terminal state/)).toHaveCount(0);
  await page.getByRole("button", { name: "Post settlement…" }).click();
  await expect(page.getByText("Pinned record version")).toBeVisible();
  expect(state.exitReads).toBeGreaterThan(0);

  // Typed confirmation: the danger button starts DISABLED; nothing has
  // reached the wire yet.
  await page.getByLabel("Payout channel").selectOption("mpesa");
  await page.getByRole("button", { name: "Post settlement…" }).click();
  const confirmButton = page.getByRole("button", { name: "Post settlement", exact: true });
  await expect(confirmButton).toBeDisabled();
  expect(state.settleBodies).toHaveLength(0);
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await confirmButton.click();

  // The result panel renders the SERVER's outcome verbatim…
  await expect(page.getByText("Settlement posted · ref EX-0001")).toBeVisible();
  // …the affordance is spent…
  await expect(page.getByRole("button", { name: "Post settlement…" })).toHaveCount(0);

  // …and exactly ONE write reached the wire: the body carries the
  // pinned snapshot version and the CASH channel and NOTHING else
  // (key-exact — no money parameter can even be sent), secrets ride as
  // HEADERS, the query string is EMPTY.
  expect(state.settleBodies).toHaveLength(1);
  expect(state.settleBodies[0]).toEqual({ version: 7, channel: "mpesa" });
  expect(state.settleHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.settleHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
  expect(new URL(state.settleUrls[0] ?? "").search).toBe("");
});

test("adversarial: settlement drift 409 → ONE attempt + conflict banner + explicit reload (never replays); the re-entered intent carries a ROTATED Idempotency-Key at the wire", async ({
  page,
}) => {
  let settlementStatus = 409;
  const state: ApiState = {
    postSettlement: () =>
      settlementStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-drift" }]
        : [201, SETTLED_RESULT],
    settleBodies: [],
    settleHeaders: [],
    settleUrls: [],
    exitReads: 0,
    exitsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Member exit" }).click();
  await page.getByText("KES 134,500.20").click();
  await page.getByRole("button", { name: "Post settlement…" }).click();
  await expect(page.getByText("Pinned record version")).toBeVisible();

  await attemptSettlement(page);

  // The drift 409: conflict banner, the P12 remedy stated, and the
  // write was attempted exactly ONCE (no auto-retry, nothing posted).
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.settleBodies).toHaveLength(1);
  const staleKey = state.settleHeaders[0]?.["idempotency-key"];

  // Explicit reload: the record re-fetches; NOTHING is replayed.
  const readsBeforeReload = state.exitReads;
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByText(/void this request and raise a fresh one/)).toBeVisible();
  expect(state.exitReads).toBeGreaterThan(readsBeforeReload);
  expect(state.settleBodies).toHaveLength(1);

  // The re-entered identical intent after the acknowledged conflict is
  // a NEW intent: the key ROTATES at the wire (!60 F3) — a backend
  // idempotency store pinned to the conflicted attempt can never serve
  // the stale outcome.
  settlementStatus = 201;
  await attemptSettlement(page);

  await expect(page.getByText("Settlement posted · ref EX-0001")).toBeVisible();
  expect(state.settleBodies).toHaveLength(2);
  expect(state.settleBodies[1]).toEqual({ version: 7, channel: "mpesa" });
  const freshKey = state.settleHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without the members module gets no Member exit nav entry, an Access denied guard on the direct URL and ZERO exits calls", async ({
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
    postSettlement: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    settleBodies: [],
    settleHeaders: [],
    settleUrls: [],
    exitReads: 0,
    exitsCalls: 0,
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the Member exit entry never mounts.
  await expect(page.getByRole("link", { name: "Transactions" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Member exit" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard — and the
  // stripped role triggers ZERO /member-exits requests.
  await page.goto("/modules/members/exits");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Member exit register")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Request exit" })).toHaveCount(0);
  expect(state.exitsCalls).toBe(0);
  expect(state.settleBodies).toHaveLength(0);
});
