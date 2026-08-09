/**
 * Recovery worklist E2E (P15 exit criterion — issue #31 batch 1,
 * audit #30 finding R1: the P13.16 collections & recovery worklist),
 * driven through the REAL production build in a real browser — OTP
 * login UI, session custody, deny-by-default guards, generated
 * client, the keyset worklist and the case-file drawer.
 *
 * The API is mocked at the BROWSER network boundary (page.route):
 * request counts/bodies/headers are asserted server-side-of-the-wire —
 * the assign single-write proof (body is exactly {version,
 * assignee_id}), the disposition 409 no-replay proof and the
 * least-disclosure page sweep (NO money renders anywhere) measure real
 * network and DOM effects.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const CASE_ID = "cccccccc-1111-2222-3333-444444444444";

/** ConfirmDangerModal phrase for the disposition (record id prefix). */
const PHRASE = CASE_ID.slice(0, 8);

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
    { module: "loan_book", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

const OPEN_CASE = {
  id: CASE_ID,
  loan_id: LOAN_ID,
  status: "open",
  assignee_id: null,
  opened_by: OFFICER_ID,
  classification_at_open: "doubtful",
  days_past_due_at_open: 120,
  opened_at: "2026-08-01T09:00:00+00:00",
  first_assigned_at: null,
  closed_at: null,
  version: 2,
};

const WORKLIST_ROW = {
  case_id: CASE_ID,
  loan_id: LOAN_ID,
  member_id: MEMBER_ID,
  days_past_due: 120,
  classification: "doubtful",
  assignee_id: null,
  assignee_unassignable: false,
  opened_at: "2026-08-01T09:00:00+00:00",
  first_assigned_at: null,
  version: 2,
};

interface ApiState {
  /** Permissions served to /me/permissions (defaults to full). */
  permissions?: unknown;
  /** The record the case GET serves — successful writes UPDATE it, so
   * the drawer's post-mutation refetch sees the server's new truth
   * (the invalidate-and-refetch flow is the real production path). */
  caseRecord: Record<string, unknown>;
  /** Returns [status, body] for the assign POST. */
  postAssign: (body: Record<string, unknown>) => [number, unknown];
  /** Returns [status, body] for the disposition POST. */
  postDisposition: (body: Record<string, unknown>) => [number, unknown];
  assignBodies: Record<string, unknown>[];
  assignHeaders: Record<string, string>[];
  dispositionBodies: Record<string, unknown>[];
  dispositionHeaders: Record<string, string>[];
  caseReads: number;
  /** EVERY request touching /recovery-cases (deny-by-default proof). */
  recoveryCalls: number;
  /** Every worklist GET url — the declared-filter wire proof
   * (issue #31 ledger (a).3). */
  worklistUrls: string[];
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

    if (path.startsWith("/recovery-cases")) state.recoveryCalls += 1;

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
    if (path === "/recovery-cases" && method === "GET") {
      state.worklistUrls.push(request.url());
      // The DECLARED filters narrow the same keyset walk (issue #31
      // ledger (a).3): a status filter serves the matching posture so
      // the DOM leg can observe a real narrowed register.
      if (url.searchParams.get("status") === "disputed") {
        await respond(200, {
          items: [
            {
              ...WORKLIST_ROW,
              case_id: "dddddddd-2222-3333-4444-555555555555",
              days_past_due: 200,
              classification: "loss",
            },
          ],
          next_cursor: null,
        });
        return;
      }
      await respond(200, { items: [WORKLIST_ROW], next_cursor: null });
      return;
    }
    if (path === `/recovery-cases/${CASE_ID}` && method === "GET") {
      state.caseReads += 1;
      await respond(200, state.caseRecord);
      return;
    }
    if (path === `/recovery-cases/${CASE_ID}/assign` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.assignBodies.push(body);
      state.assignHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postAssign(body);
      // A committed write becomes the record every later read serves.
      if (status === 200) state.caseRecord = responseBody as Record<string, unknown>;
      await respond(status, responseBody);
      return;
    }
    if (path === `/recovery-cases/${CASE_ID}/disposition` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.dispositionBodies.push(body);
      state.dispositionHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.postDisposition(body);
      if (status === 200) state.caseRecord = responseBody as Record<string, unknown>;
      await respond(status, responseBody);
      return;
    }
    if (path === `/recovery-cases/${CASE_ID}/notes` && method === "GET") {
      await respond(200, { items: [], next_cursor: null });
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

/** Arm one pause-disposition attempt through the typed confirmation. */
async function attemptPause(page: Page): Promise<void> {
  await page.getByLabel("Disposition").selectOption("disputed");
  await page
    .getByLabel("Reason (required to pause)")
    .fill("Member disputes the arrears figures");
  await page.getByRole("button", { name: "Record disposition…" }).click();
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await page.getByRole("button", { name: "Record disposition", exact: true }).click();
}

test("happy path: OTP login → Recovery worklist renders workflow facts with NO money anywhere → case drawer → Assign to me commits exactly ONE wire POST with {version, assignee_id} ONLY and the Idempotency-Key as a HEADER", async ({
  page,
}) => {
  const state: ApiState = {
    caseRecord: { ...OPEN_CASE },
    postAssign: (body) => [
      200,
      {
        ...OPEN_CASE,
        assignee_id: body["assignee_id"],
        first_assigned_at: "2026-08-02T10:00:00+00:00",
        version: 3,
      },
    ],
    postDisposition: () => [200, OPEN_CASE],
    assignBodies: [],
    assignHeaders: [],
    dispositionBodies: [],
    dispositionHeaders: [],
    caseReads: 0,
    recoveryCalls: 0,
    worklistUrls: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Recovery" }).click();
  // The worklist renders workflow facts…
  await expect(page.getByText("Recovery worklist")).toBeVisible();
  await expect(page.getByText("120")).toBeVisible();
  // Scoped to the register table: the (a).3 classification filter now
  // ALSO carries the label as an <option> — assert the rendered ROW.
  await expect(page.getByRole("region", { name: "Table" }).getByText("Doubtful")).toBeVisible();
  await expect(page.getByText("— (unassigned)")).toBeVisible();
  // …and LEAST DISCLOSURE holds across the whole page (addendum A5):
  // no money figure exists anywhere in the DOM.
  await expect(page.locator("body")).not.toContainText("KES");

  // Row drill-down → the case drawer (fresh record read).
  await page.getByText("120").click();
  await expect(page.getByText("Record version")).toBeVisible();
  expect(state.caseReads).toBeGreaterThan(0);
  // Attribution: the opener's SHORT id under least disclosure.
  await expect(page.getByTitle(OFFICER_ID).first()).toHaveText(OFFICER_ID.slice(0, 8));
  // Still no money anywhere with the drawer open.
  await expect(page.locator("body")).not.toContainText("KES");

  // Assign to me: exactly ONE wire POST, the body is KEY-EXACT
  // {version, assignee_id} (no timestamp can even be sent — A7),
  // secrets ride as HEADERS.
  await page.getByRole("button", { name: "Assign to me" }).click();
  await expect(page.getByTitle(ADMIN_ID)).toBeVisible();
  expect(state.assignBodies).toHaveLength(1);
  expect(state.assignBodies[0]).toEqual({ version: 2, assignee_id: ADMIN_ID });
  expect(state.assignHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.assignHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("declared worklist filters (issue #31 ledger (a).3): the DEFAULT view sends NO filter key at all; the pause segment and classification select bind the two DECLARED params ONLY — the narrowed register renders the server's rows, still no money anywhere", async ({
  page,
}) => {
  const state: ApiState = {
    caseRecord: { ...OPEN_CASE },
    postAssign: () => [200, OPEN_CASE],
    postDisposition: () => [200, OPEN_CASE],
    assignBodies: [],
    assignHeaders: [],
    dispositionBodies: [],
    dispositionHeaders: [],
    caseReads: 0,
    recoveryCalls: 0,
    worklistUrls: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Recovery" }).click();
  await expect(page.getByText("120")).toBeVisible();

  // The DEFAULT view sent NO filter key — byte-identical to the
  // as-built read (keyset limit only; no offset/page ever).
  expect(state.worklistUrls.length).toBeGreaterThan(0);
  const defaultParams = new URL(state.worklistUrls[0] ?? "").searchParams;
  expect(defaultParams.has("status")).toBe(false);
  expect(defaultParams.has("classification")).toBe(false);
  expect(defaultParams.has("offset")).toBe(false);
  expect(defaultParams.get("limit")).toBeTruthy();

  // Narrow to the disputed posture: the DECLARED param binds the
  // code-owned vocabulary value verbatim and the register re-renders
  // with the SERVER's narrowed rows (never a local re-sort).
  await page.getByRole("button", { name: "Paused — disputed" }).click();
  await expect(page.getByText("200")).toBeVisible();
  // Scoped to the register table (the filter <option> also says "Loss").
  await expect(page.getByRole("region", { name: "Table" }).getByText("Loss")).toBeVisible();
  const filtered = state.worklistUrls[state.worklistUrls.length - 1] ?? "";
  const filteredParams = new URL(filtered).searchParams;
  expect(filteredParams.get("status")).toBe("disputed");
  expect(filteredParams.has("classification")).toBe(false);

  // Add the classification filter: BOTH declared params, nothing else
  // beside the keyset keys (an undeclared key stays a server 422 —
  // and can never even be constructed by the generated client).
  await page.getByLabel("Classification").selectOption("loss");
  await expect
    .poll(() => {
      const last = new URL(state.worklistUrls[state.worklistUrls.length - 1] ?? "");
      return [...last.searchParams.keys()].sort().join(",");
    })
    .toBe("classification,limit,status");

  // Least disclosure survives every filtered view (addendum A5).
  await expect(page.locator("body")).not.toContainText("KES");
});

test("adversarial: disposition 409 (the single domain gatekeeper refused / version drift) → ONE attempt + conflict banner + explicit reload re-fetches (never replays); the pause body carries {version, status, reason} and NO note key", async ({
  page,
}) => {
  let dispositionStatus = 409;
  const state: ApiState = {
    caseRecord: { ...OPEN_CASE },
    postAssign: () => [200, OPEN_CASE],
    postDisposition: (body) =>
      dispositionStatus === 409
        ? [409, { category: "conflict", correlation_id: "corr-e2e-gate" }]
        : [200, { ...OPEN_CASE, status: body["status"], version: 3 }],
    assignBodies: [],
    assignHeaders: [],
    dispositionBodies: [],
    dispositionHeaders: [],
    caseReads: 0,
    recoveryCalls: 0,
    worklistUrls: [],
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Recovery" }).click();
  await page.getByText("120").click();
  await expect(page.getByText("Record version")).toBeVisible();

  await attemptPause(page);

  // The gatekeeper 409: conflict banner, exactly ONE attempt, and the
  // target-paired body (!53 F2) — the reason rides, the note key does
  // NOT exist on a pause.
  await expect(page.getByText(/Your change was NOT applied/)).toBeVisible();
  expect(state.dispositionBodies).toHaveLength(1);
  expect(state.dispositionBodies[0]).toEqual({
    version: 2,
    status: "disputed",
    reason: "Member disputes the arrears figures",
  });
  const staleKey = state.dispositionHeaders[0]?.["idempotency-key"];

  // Explicit reload: the record re-fetches; NOTHING is replayed.
  const readsBeforeReload = state.caseReads;
  await page.getByRole("button", { name: "Reload record" }).click();
  await expect(page.getByText(/re-check the case status before acting again/)).toBeVisible();
  expect(state.caseReads).toBeGreaterThan(readsBeforeReload);
  expect(state.dispositionBodies).toHaveLength(1);

  // The re-entered identical intent is a NEW one: the key ROTATES at
  // the wire (!60 F3).
  dispositionStatus = 200;
  await page.getByRole("button", { name: "Record disposition…" }).click();
  await page.getByLabel(`Type "${PHRASE}" to confirm`).fill(PHRASE);
  await page.getByRole("button", { name: "Record disposition", exact: true }).click();

  // Scoped to the case drawer: the (a).3 status segment in the toolbar
  // ALSO says "Paused — disputed" — assert the DRAWER's status pill.
  await expect(page.getByLabel("Recovery case").getByText("Paused — disputed")).toBeVisible();
  expect(state.dispositionBodies).toHaveLength(2);
  const freshKey = state.dispositionHeaders[1]?.["idempotency-key"];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("adversarial: deny-by-default — a role without the loan_book module gets no Recovery nav entry, an Access denied guard on the direct URL and ZERO recovery-cases calls", async ({
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
    caseRecord: { ...OPEN_CASE },
    postAssign: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    postDisposition: () => [403, { category: "forbidden", correlation_id: "corr-e2e-403" }],
    assignBodies: [],
    assignHeaders: [],
    dispositionBodies: [],
    dispositionHeaders: [],
    caseReads: 0,
    recoveryCalls: 0,
    worklistUrls: [],
  };
  await mockApi(page, state);
  await login(page);

  // Permission-filtered nav: the Recovery entry never mounts.
  await expect(page.getByRole("link", { name: "Transactions" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Recovery" })).toHaveCount(0);

  // Direct navigation hits the deny-by-default route guard — and the
  // stripped role triggers ZERO /recovery-cases requests.
  await page.goto("/modules/loan_book/recovery");
  await expect(page.getByText("Access denied")).toBeVisible();
  await expect(page.getByText("Recovery worklist")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "+ Initiate recovery" })).toHaveCount(0);
  expect(state.recoveryCalls).toBe(0);
  expect(state.assignBodies).toHaveLength(0);
});
