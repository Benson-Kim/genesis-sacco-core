/**
 * Member-KYC E2E (issue #31 batch 3): the registration wizard, the KYC
 * drawer and the documents checklist driven through the REAL
 * production build in a real browser, extending the existing
 * `page.route` harness (never rebuilt).
 *
 * - happy: row drill-down renders the SERVER profile verbatim
 *   (including the informational income string), the checklist mirrors
 *   the status machine, and a review move commits EXACTLY ONE
 *   version-pinned PUT with an Idempotency-Key header (wire proof).
 * - happy (wizard): a profile-less member gets the full wizard; the
 *   create commits EXACTLY ONE key-exact POST (category + consent +
 *   nested per-type payload; the member type is NEVER in the body).
 * - forbidden-role: members:view only — every write affordance
 *   (edit, wizard, track, review, expiry) is structurally absent.
 * - adversarial: a stale profile edit 409s into the shared
 *   ConflictBanner; ONE attempt, reload REFETCHES and nothing is
 *   replayed.
 */
import { expect, test, type Page } from "@playwright/test";

const API_ORIGIN = "http://localhost:8000";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PROFILE_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const DOC_ID_1 = "bbbbbbbb-1111-2222-3333-444444444444";
const DOC_ID_2 = "cccccccc-1111-2222-3333-444444444444";

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

const VIEW_ONLY_PERMISSIONS = {
  role_id: "66666666-6666-6666-6666-666666666666",
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

const MEMBER_OUT = {
  id: MEMBER_ID,
  member_no: "GP-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: "+254700000001",
  email: "jane@sacco.co.ke",
  status: "active",
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

// DETAIL read (#31 batch 3): the drawer's financial summary renders
// these four SERVER strings verbatim. DELIBERATELY NON-ADDITIVE: a
// screen that summed figures would surface a string asserted ABSENT.
const MEMBER_DETAIL_OUT = {
  ...MEMBER_OUT,
  aggregates: {
    deposits_total: "1000.11",
    shares_total: "200.22",
    loans_outstanding: "300.33",
    guarantees_pledged: "40.04",
  },
};

const PERSON_PROFILE_PAYLOAD = {
  bio: {
    first_name: "Jane",
    surname: "Wanjiku",
    gender: "Female",
    date_of_birth: "1990-01-01",
    id_number: "12345678",
    kra_pin: "A012345678Z",
  },
  contact: {
    phone: "+254700000001",
    email: null,
    county: "Nairobi",
    physical_address: "Moi Avenue 10",
  },
  employment: {
    employment_status: "Employed",
    occupation: "Teacher",
    monthly_income: "123456.70",
  },
  next_of_kin: { name: "Peter Wanjiku", relationship: "Brother", phone: "+254700000002" },
};

const PROFILE_OUT = {
  id: PROFILE_ID,
  member_id: MEMBER_ID,
  member_type: "person",
  category: "Ordinary",
  profile: PERSON_PROFILE_PAYLOAD,
  // Widened: the wizard mock rewrites this branch (null when consent
  // was not captured with the create).
  dpa_consent_at: "2026-08-01T09:00:00+00:00" as string | null,
  version: 3,
  created_at: "2026-08-01T09:00:00+00:00",
};

const DOCUMENTS_OUT = [
  {
    id: DOC_ID_1,
    member_id: MEMBER_ID,
    doc_type: "national_id_copy",
    status: "pending",
    expires_at: null,
    version: 1,
    created_at: "2026-08-01T09:00:00+00:00",
  },
  {
    id: DOC_ID_2,
    member_id: MEMBER_ID,
    doc_type: "kra_pin",
    status: "verified",
    expires_at: "2027-01-01",
    version: 2,
    created_at: "2026-08-01T09:00:00+00:00",
  },
];

interface ApiState {
  permissions: unknown;
  /** null = the member has no profile yet (404 branch). */
  profile: typeof PROFILE_OUT | null;
  profileGetCalls: number;
  profilePostBodies: Record<string, unknown>[];
  profilePostHeaders: Record<string, string>[];
  profilePutBodies: Record<string, unknown>[];
  profilePutHeaders: Record<string, string>[];
  profilePutStatus: number;
  documentPutBodies: Record<string, unknown>[];
  documentPutHeaders: Record<string, string>[];
}

function initialState(overrides: Partial<ApiState> = {}): ApiState {
  return {
    permissions: FULL_PERMISSIONS,
    profile: PROFILE_OUT,
    profileGetCalls: 0,
    profilePostBodies: [],
    profilePostHeaders: [],
    profilePutBodies: [],
    profilePutHeaders: [],
    profilePutStatus: 200,
    documentPutBodies: [],
    documentPutHeaders: [],
    ...overrides,
  };
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
      await respond(200, state.permissions);
      return;
    }
    if (path === "/members" && method === "GET") {
      // #31 batch 3 review: rows carry the aggregates object ONLY when
      // the register opted in with include=aggregates.
      const include = new URL(request.url()).searchParams.get("include");
      await respond(200, {
        items: [include === "aggregates" ? MEMBER_DETAIL_OUT : MEMBER_OUT],
        next_cursor: null,
      });
      return;
    }
    if (path === `/members/${MEMBER_ID}` && method === "GET") {
      await respond(200, MEMBER_DETAIL_OUT);
      return;
    }
    if (path === `/members/${MEMBER_ID}/profile` && method === "GET") {
      state.profileGetCalls += 1;
      if (state.profile === null) {
        await respond(404, { category: "not_found", correlation_id: "corr-e2e-404" });
        return;
      }
      await respond(200, state.profile);
      return;
    }
    if (path === `/members/${MEMBER_ID}/profile` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.profilePostBodies.push(body);
      state.profilePostHeaders.push(await request.allHeaders());
      state.profile = {
        ...PROFILE_OUT,
        category: body["category"] as string,
        profile: body["profile"] as typeof PERSON_PROFILE_PAYLOAD,
        dpa_consent_at: body["dpa_consent"] === true ? "2026-08-05T10:00:00+00:00" : null,
        version: 1,
      };
      await respond(201, state.profile);
      return;
    }
    if (path === `/members/${MEMBER_ID}/profile` && method === "PUT") {
      state.profilePutBodies.push(request.postDataJSON() as Record<string, unknown>);
      state.profilePutHeaders.push(await request.allHeaders());
      if (state.profilePutStatus === 409) {
        await respond(409, { category: "conflict", correlation_id: "corr-e2e-409" });
        return;
      }
      await respond(200, { ...PROFILE_OUT, version: 4 });
      return;
    }
    if (path === `/members/${MEMBER_ID}/documents` && method === "GET") {
      await respond(200, { items: DOCUMENTS_OUT, next_cursor: null });
      return;
    }
    if (path === `/members/${MEMBER_ID}/documents` && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      await respond(201, {
        id: "dddddddd-1111-2222-3333-444444444444",
        member_id: MEMBER_ID,
        doc_type: body["doc_type"],
        status: "pending",
        expires_at: null,
        version: 1,
        created_at: "2026-08-05T10:00:00+00:00",
      });
      return;
    }
    if (path === `/members/${MEMBER_ID}/documents/${DOC_ID_1}` && method === "PUT") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.documentPutBodies.push(body);
      state.documentPutHeaders.push(await request.allHeaders());
      await respond(200, { ...DOCUMENTS_OUT[0], status: body["status"] ?? "pending", version: 2 });
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

async function openKycDrawer(page: Page) {
  await page.getByRole("link", { name: "Members" }).click();
  await page.getByText("Jane Wanjiku").click();
  return page.getByRole("dialog", { name: "Member KYC — Jane Wanjiku" });
}

test("happy path: row drill-down renders the SERVER profile verbatim + the checklist; a review move is ONE version-pinned PUT with an Idempotency-Key header", async ({
  page,
}) => {
  const state = initialState();
  await mockApi(page, state);
  await login(page);

  const drawer = await openKycDrawer(page);
  await expect(drawer.getByText("Ordinary")).toBeVisible();
  await expect(drawer.getByText(/Captured 2026-08-01/)).toBeVisible();
  // Informational declared figure renders VERBATIM (no KES prefix, no
  // grouping — a client re-format would render something else).
  await expect(drawer.getByText("123456.70", { exact: true })).toBeVisible();
  await expect(drawer.getByText("A012345678Z")).toBeVisible();

  // Financial summary (#31 batch 3): the four SERVER aggregate strings
  // verbatim; the non-additive fixture proves nothing is summed (a
  // derived figure would read 1200.33 or 1540.70 — asserted absent).
  await expect(drawer.getByText("Financial summary")).toBeVisible();
  await expect(drawer.getByText("1000.11", { exact: true })).toBeVisible();
  await expect(drawer.getByText("200.22", { exact: true })).toBeVisible();
  await expect(drawer.getByText("300.33", { exact: true })).toBeVisible();
  await expect(drawer.getByText("40.04", { exact: true })).toBeVisible();
  await expect(drawer.getByText("1200.33")).toHaveCount(0);
  await expect(drawer.getByText("1540.70")).toHaveCount(0);

  // Checklist mirrors the status machine: pending offers ONLY the
  // handover move; verified is terminal.
  await expect(drawer.getByText("National ID copy")).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Mark received" })).toHaveCount(1);
  await expect(drawer.getByRole("button", { name: "Verify" })).toHaveCount(0);

  await drawer.getByRole("button", { name: "Mark received" }).click();
  await expect
    .poll(() => state.documentPutBodies.length, { message: "document review PUT" })
    .toBe(1);
  // Version-pinned, single-purpose body (exclude-unset: NO expiry key).
  expect(state.documentPutBodies[0]).toEqual({ version: 1, status: "received" });
  expect(state.documentPutHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.documentPutHeaders[0]?.["authorization"]).toMatch(/^Bearer /);
});

test("happy path (wizard): a profile-less member gets the full per-type wizard; the create commits EXACTLY ONE key-exact POST", async ({
  page,
}) => {
  const state = initialState({ profile: null });
  await mockApi(page, state);
  await login(page);

  const drawer = await openKycDrawer(page);
  await expect(drawer.getByText(/No KYC profile on record/)).toBeVisible();
  await drawer.getByRole("button", { name: "Start KYC wizard" }).click();

  const wizard = page.getByRole("dialog", { name: "New member registration — Jane Wanjiku" });
  await wizard.getByLabel("First name").fill("Jane");
  await wizard.getByLabel("Surname").fill("Wanjiku");
  await wizard.getByLabel("Gender").fill("Female");
  await wizard.getByLabel("Date of birth").fill("1990-01-01");
  await wizard.getByLabel("ID number").fill("12345678");
  await wizard.getByLabel("KRA PIN").fill("A012345678Z");
  await wizard.getByLabel("Phone (M-Pesa)").fill("+254700000001");
  await wizard.getByLabel("County").fill("Nairobi");
  await wizard.getByLabel("Physical address").fill("Moi Avenue 10");
  await wizard.getByLabel("Employment status").fill("Employed");
  await wizard.getByLabel("Occupation").fill("Teacher");
  await wizard.getByLabel("Monthly income (KES)").fill("35000.50");
  await wizard.getByLabel("Name", { exact: true }).fill("Peter Wanjiku");
  await wizard.getByLabel("Relationship").fill("Brother");
  await wizard.getByLabel("Phone", { exact: true }).fill("+254700000002");
  await wizard.getByRole("button", { name: "Continue" }).click();

  await wizard.getByLabel("Member category").fill("Ordinary");
  await wizard.getByLabel(/Data Protection Act 2019 consent/).check();
  await wizard.getByRole("button", { name: "Create KYC profile" }).click();

  // The wizard advanced to documents & review on the SERVER response.
  await expect(wizard.getByText("Summary")).toBeVisible();
  await expect(wizard.getByText("Captured ✓")).toBeVisible();

  // Wire proof: ONE POST; category + consent + the nested payload and
  // NOTHING else (the member type is never caller-supplied).
  expect(state.profilePostBodies).toHaveLength(1);
  const body = state.profilePostBodies[0]!;
  expect(Object.keys(body).sort()).toEqual(["category", "dpa_consent", "profile"]);
  expect(body["category"]).toBe("Ordinary");
  expect(body["dpa_consent"]).toBe(true);
  const profile = body["profile"] as Record<string, Record<string, unknown>>;
  expect(profile["employment"]).toEqual({
    employment_status: "Employed",
    occupation: "Teacher",
    monthly_income: "35000.50",
  });
  expect(profile["contact"]?.["email"]).toBeNull();
  expect(state.profilePostHeaders[0]?.["idempotency-key"]).toBeTruthy();
  expect(state.profilePostHeaders[0]?.["x-tenant-id"]).toBeTruthy();
});

test("forbidden-role: members:view only — every KYC write affordance is structurally absent", async ({
  page,
}) => {
  const state = initialState({ permissions: VIEW_ONLY_PERMISSIONS });
  await mockApi(page, state);
  await login(page);

  const drawer = await openKycDrawer(page);
  await expect(drawer.getByText("Ordinary")).toBeVisible();
  await expect(drawer.getByText("National ID copy")).toBeVisible();
  // Read-only: no edit, no review moves, no expiry editor, no track.
  await expect(drawer.getByRole("button", { name: "Edit profile" })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "Mark received" })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: "Edit expiry" })).toHaveCount(0);
  await expect(drawer.getByRole("button", { name: /^Track / })).toHaveCount(0);
});

test("adversarial: a stale profile edit 409s into the ConflictBanner — ONE attempt, reload REFETCHES, nothing is replayed", async ({
  page,
}) => {
  const state = initialState({ profilePutStatus: 409 });
  await mockApi(page, state);
  await login(page);

  const drawer = await openKycDrawer(page);
  await drawer.getByRole("button", { name: "Edit profile" }).click();
  await drawer.getByLabel("Occupation").fill("Nurse");
  const getsBefore = state.profileGetCalls;
  await drawer.getByRole("button", { name: "Save profile" }).click();

  await expect(drawer.getByText(/This record changed since you loaded it/)).toBeVisible();
  expect(state.profilePutBodies).toHaveLength(1);
  // The stale submission pinned the version it loaded.
  expect(state.profilePutBodies[0]?.["version"]).toBe(3);

  await drawer.getByRole("button", { name: "Reload record" }).click();
  // Reload is a READ — the write count must not move.
  await expect
    .poll(() => state.profileGetCalls, { message: "profile refetch after conflict" })
    .toBeGreaterThan(getsBefore);
  expect(state.profilePutBodies).toHaveLength(1);
  // Re-applying is a NEW explicit intent: the edit form closed.
  await expect(drawer.getByRole("button", { name: "Edit profile" })).toBeVisible();
});
