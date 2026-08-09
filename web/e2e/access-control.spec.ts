/**
 * Access-control E2E (P15 exit criterion backfill — issue #30 T1/T3:
 * the permissions-matrix editor is the highest-value untested surface):
 * happy path (users tab + matrix save proven at the browser wire) + the
 * self-privilege-escalation adversarial flow, driven through the REAL
 * production build in a real browser.
 *
 * The API is mocked at the BROWSER network boundary (page.route on the
 * API origin), extending the existing harness pattern (never rebuilt).
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const ROLE_TELLER = "66666666-6666-6666-6666-666666666666";
const ROLE_ADMIN = "77777777-7777-7777-7777-777777777777";

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
  role_id: ROLE_ADMIN,
  permissions: [
    {
      module: "access_control",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

const ROLES_OUT = [
  { id: ROLE_TELLER, name: "Teller", is_system: true },
  { id: ROLE_ADMIN, name: "System Admin", is_system: true },
];

const USER_OUT = {
  id: "55555555-5555-5555-5555-555555555555",
  full_name: "Jane Wanjiku",
  email: "jane@sacco.co.ke",
  phone: null,
  branch: null,
  role_id: ROLE_TELLER,
  role_name: "Teller",
  status: "active",
  last_active_at: null,
  version: 1,
};

/** The Teller role's server matrix (first role auto-selects): members
 * view-only; every other module has no row (deny-by-default render). */
function tellerPermissions(canCreateMembers: boolean) {
  return [
    {
      module: "members",
      can_view: true,
      can_create: canCreateMembers,
      can_edit: false,
      can_approve: false,
    },
  ];
}

interface ApiState {
  /** Returns [status, body] for the permission PUT. */
  putPermission: (module: string, body: Record<string, unknown>) => [number, unknown];
  putBodies: { module: string; body: Record<string, unknown> }[];
  putHeaders: Record<string, string>[];
  tellerCanCreateMembers: boolean;
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
      await respond(200, FULL_PERMISSIONS);
      return;
    }
    if (path === "/users" && method === "GET") {
      await respond(200, { items: [USER_OUT], next_cursor: null });
      return;
    }
    if (path === "/access/roles" && method === "GET") {
      await respond(200, ROLES_OUT);
      return;
    }
    if (path === `/access/roles/${ROLE_TELLER}/permissions` && method === "GET") {
      await respond(200, tellerPermissions(state.tellerCanCreateMembers));
      return;
    }
    if (path === `/access/roles/${ROLE_ADMIN}/permissions` && method === "GET") {
      await respond(200, [
        {
          module: "access_control",
          can_view: true,
          can_create: true,
          can_edit: true,
          can_approve: true,
        },
      ]);
      return;
    }
    const putMatch = new RegExp(
      `^/access/roles/${ROLE_TELLER}/permissions/([a-z_]+)$`,
    ).exec(path);
    if (putMatch !== null && method === "PUT") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.putBodies.push({ module: putMatch[1]!, body });
      state.putHeaders.push(await request.allHeaders());
      const [status, responseBody] = state.putPermission(putMatch[1]!, body);
      await respond(status, responseBody);
      return;
    }
    await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
  });
}

/** Drive the REAL OTP login UI. */
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill("admin@sacco.co.ke");
  await page.getByRole("button", { name: "Send OTP" }).click();
  for (let index = 1; index <= 6; index += 1) {
    await page.getByLabel(`Digit ${index}`).fill(String(index));
  }
  await page.getByRole("button", { name: "Verify & sign in" }).click();
  await page.waitForURL("**/dashboard");
}

test("happy path: OTP login → users tab lists accounts → the permissions matrix saves ONE key-exact absolute PUT with an Idempotency-Key (wire proof)", async ({
  page,
}) => {
  const state: ApiState = {
    putPermission: (module, body) => {
      state.tellerCanCreateMembers = body["can_create"] === true;
      return [200, { module, ...body }];
    },
    putBodies: [],
    putHeaders: [],
    tellerCanCreateMembers: false,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Access control" }).click();
  // Users tab (default) lists the server's accounts verbatim.
  await expect(page.getByText("Jane Wanjiku")).toBeVisible();

  // The permissions matrix: first role auto-selects; server rows render
  // their exact bits and absent rows render deny-by-default.
  await page.getByRole("tab", { name: "Permissions" }).click();
  await expect(page.getByText("Permissions — Teller")).toBeVisible();
  const membersView = page.getByRole("checkbox", { name: "Members — View" });
  await expect(membersView).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Reports — Approve" })).not.toBeChecked();

  // Toggle one bit and save.
  await page.getByRole("checkbox", { name: "Members — Create" }).check();
  await expect(page.getByText("1 unsaved change")).toBeVisible();
  await page.getByRole("button", { name: "Save role" }).click();

  // Wire proof: exactly ONE PUT, to the ONE dirty module, carrying the
  // ABSOLUTE four-bit body and nothing else, with the Idempotency-Key
  // as a header next to bearer + tenant.
  await expect
    .poll(() => state.putBodies.length, { message: "exactly one permission PUT" })
    .toBe(1);
  expect(state.putBodies[0]?.module).toBe("members");
  expect(state.putBodies[0]?.body).toEqual({
    can_view: true,
    can_create: true,
    can_edit: false,
    can_approve: false,
  });
  expect(state.putHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.putHeaders[0]?.["authorization"]).toMatch(/^Bearer /);

  // The refetched server truth keeps the committed bit; dirty state
  // clears, so the save affordance disarms (no second write possible).
  await expect(page.getByRole("checkbox", { name: "Members — Create" })).toBeChecked();
  await expect(page.getByRole("button", { name: "Save role" })).toBeDisabled();
  expect(state.putBodies).toHaveLength(1);
});

test("adversarial self-privilege-escalation: the SERVER's 403 refusal surfaces as ONE least-disclosure banner; no retry, no local pretend-success, session intact", async ({
  page,
}) => {
  const state: ApiState = {
    putPermission: () => [403, { category: "forbidden", correlation_id: "corr-e2e-esc" }],
    putBodies: [],
    putHeaders: [],
    tellerCanCreateMembers: false,
  };
  await mockApi(page, state);
  await login(page);

  await page.getByRole("link", { name: "Access control" }).click();
  await page.getByRole("tab", { name: "Permissions" }).click();
  await expect(page.getByText("Permissions — Teller")).toBeVisible();

  // The escalation probe: grant access_control bits to a lesser role
  // (the classic pivot: escalate a role you can assume). The SERVER
  // refuses — the UI must surface the sanitized verdict only.
  await page.getByRole("checkbox", { name: "Access control — Edit / Post" }).check();
  await page.getByRole("button", { name: "Save role" }).click();

  await expect(page.getByText("Not permitted.")).toBeVisible();
  await expect(page.getByText("ref: corr-e2e-esc")).toBeVisible();
  // The raw server category never renders (no capability probing).
  await expect(page.getByText(/forbidden/i)).toHaveCount(0);
  await expect(page.getByText("Save failed — retry")).toBeVisible();
  // Exactly ONE attempt at the wire — a denial is never auto-retried.
  expect(state.putBodies).toHaveLength(1);
  expect(state.putBodies[0]?.module).toBe("access_control");
  // The refused bits never render as granted server truth: re-entering
  // the role re-reads the matrix and the bit is still off.
  await page.getByRole("button", { name: "System Admin" }).click();
  await expect(page.getByText("Permissions — System Admin")).toBeVisible();
  await page.getByRole("button", { name: "Teller", exact: true }).click();
  await expect(page.getByText("Permissions — Teller")).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "Access control — Edit / Post" }),
  ).not.toBeChecked();
  // Under-privilege is not session death: still on the module, no
  // /login redirect.
  await expect(page.getByRole("tab", { name: "Permissions" })).toBeVisible();
  expect(state.putBodies).toHaveLength(1);
});
