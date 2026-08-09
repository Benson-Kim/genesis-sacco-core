/**
 * Guarantors screen + drawers adversarial tests THROUGH THE REAL SCREEN
 * CODE (P15 Guarantors batch), mirroring the loans/applications
 * reference suites: the feature API layers are stubbed at the module
 * boundary; rendering, keyset paging, permission gating, the witnessed
 * registry, idempotency-key custody, 409 handling and the typed
 * confirmations are the real shipped code, mounted under the REAL
 * <Providers>.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { logout } from "@/modules/auth/api";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { announce } from "@/modules/layout/announcer";
import { GuarantorsScreen } from "../components/GuarantorsScreen";
import {
  guaranteeSchema,
  pledgeCreateSchema,
  substituteCreateSchema,
  substitutionSchema,
  type Guarantee,
  type Substitution,
} from "../schemas";
import {
  clearWitnessedGuarantees,
  recordWitnessedGuarantee,
} from "../sessionRegistry";
import type { Application } from "@/modules/applications/schemas";
import type { DashboardSummary } from "@/modules/dashboard/schemas";
import * as guarantorsApi from "../api";
import * as dashboardApi from "@/modules/dashboard/api";
import * as appsApi from "@/modules/applications/api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    pledgeGuarantee: jest.fn(),
    consentGuarantee: jest.fn(),
    releaseGuarantee: jest.fn(),
    substituteGuarantee: jest.fn(),
  };
});

jest.mock("@/modules/dashboard/api", () => ({
  fetchDashboardSummary: jest.fn(),
}));

jest.mock("@/modules/applications/api", () => {
  const actual = jest.requireActual<typeof import("@/modules/applications/api")>(
    "@/modules/applications/api",
  );
  return {
    ...actual,
    fetchApplicationsPage: jest.fn(),
    fetchApplication: jest.fn(),
    fetchProducts: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

// The live-region announcer is a spy so the suites can prove every
// async outcome — including the 409 withdrawal (!60 F5) — is announced.
jest.mock("@/modules/layout/announcer", () => {
  const actual = jest.requireActual<typeof import("@/modules/layout/announcer")>(
    "@/modules/layout/announcer",
  );
  return { ...actual, announce: jest.fn() };
});

const mocked = jest.mocked(guarantorsApi);
const mockedDashboard = jest.mocked(dashboardApi);
const mockedApps = jest.mocked(appsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);
const mockedAnnounce = jest.mocked(announce);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const BORROWER_ID = "11111111-1111-1111-1111-111111111111";
const GUARANTOR_ID = "22222222-1111-2222-3333-444444444444";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const GUARANTEE_ID = "bbbbbbbb-1111-2222-3333-444444444444";
const LOAN_ID = "cccccccc-1111-2222-3333-444444444444";

// Attacker-influenced free-text fields on this screen — must render as
// inert text (named XSS threat).
const HOSTILE_NAME = '<img src=x onerror=window.__pwned=1> "quoted" guarantor';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const SUMMARY: DashboardSummary = {
  as_of: "2026-08-04T06:00:00Z",
  members: null,
  deposits: null,
  loan_book: null,
  monthly_flows: null,
  pipeline: null,
  guarantors: {
    active_guarantees: 7,
    total_pledged: "1234567.10",
    distinct_guarantors: 4,
    guarantors: [
      {
        member_id: GUARANTOR_ID,
        member_no: "M-0002",
        name: "Peter Otieno",
        pledged_total: "500000.10",
        free_capacity: "250000.20",
      },
    ],
  },
};

function application(overrides: Partial<Application> = {}): Application {
  return {
    id: APP_ID,
    member_id: BORROWER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
    rate_pct: "12.50",
    purpose: "School fees",
    stage: "submitted",
    cover_pct: "80.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 2,
    ...overrides,
  };
}

function guarantee(overrides: Partial<Guarantee> = {}): Guarantee {
  return {
    id: GUARANTEE_ID,
    application_id: APP_ID,
    loan_id: null,
    borrower_member_id: BORROWER_ID,
    guarantor_member_id: GUARANTOR_ID,
    amount: "50000.10",
    status: "pledged",
    version: 1,
    ...overrides,
  };
}

const PRODUCT = {
  id: PRODUCT_ID,
  name: "Development Loan",
  rate_pct: "12.50",
  deposit_multiplier: "3.00",
  max_term_months: 48,
  guarantors_required: 2,
  active: true,
  version: 1,
};

const BORROWER = {
  id: BORROWER_ID,
  member_no: "M-0001",
  type: "person" as const,
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active" as const,
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

const GUARANTOR = {
  id: GUARANTOR_ID,
  member_no: "M-0002",
  type: "person" as const,
  name: "Peter Otieno",
  phone: null,
  email: null,
  status: "active" as const,
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "applications", can_view: true, can_create: true, can_edit: true, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "applications", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS | typeof VIEW_ONLY_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function page<T>(items: T[], nextCursor: string | null = null) {
  return { items, nextCursor };
}

function mountScreen() {
  return render(
    <Providers>
      <GuarantorsScreen />
    </Providers>,
  );
}

/** Open the pledge drawer from the queue and fill the form (shared by
 * the double-pledge / 409 / key-custody tests). */
async function openPledgeDrawer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Pledge ›"));
  const drawer = await screen.findByRole("dialog", { name: "Pledge guarantee" });
  await within(drawer).findByText("Applied amount");
  return drawer;
}

async function confirmPledge(
  user: ReturnType<typeof userEvent.setup>,
  drawer: HTMLElement,
  amount = "50000.10",
) {
  await user.selectOptions(within(drawer).getByLabelText("Guarantor"), GUARANTOR_ID);
  const amountInput = within(drawer).getByLabelText("Pledge amount (KES)");
  await user.clear(amountInput);
  await user.type(amountInput, amount);
  await user.click(within(drawer).getByRole("button", { name: "Pledge…" }));
  const confirm = await screen.findByRole("dialog", { name: "Encumber savings" });
  const phrase = APP_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  return within(confirm).getByRole("button", { name: "Pledge guarantee" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  clearWitnessedGuarantees();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mockedDashboard.fetchDashboardSummary.mockResolvedValue(SUMMARY);
  mockedApps.fetchApplicationsPage.mockResolvedValue(page([application()]));
  mockedApps.fetchApplication.mockResolvedValue(application());
  mockedApps.fetchProducts.mockResolvedValue([PRODUCT]);
  mockedMembers.fetchMembersPage.mockResolvedValue(page([BORROWER, GUARANTOR]));
  mockedMembers.fetchMember.mockResolvedValue(BORROWER);
});

afterEach(() => {
  clearSession();
  clearWitnessedGuarantees();
});

test("hostile guarantor name renders as inert TEXT; aggregate money renders VERBATIM through fmtKes (named XSS threat + blocker (a))", async () => {
  mockedDashboard.fetchDashboardSummary.mockResolvedValue({
    ...SUMMARY,
    guarantors: {
      ...SUMMARY.guarantors!,
      guarantors: [{ ...SUMMARY.guarantors!.guarantors[0]!, name: HOSTILE_NAME }],
    },
  });
  const { container } = mountScreen();

  // The payload is visible as literal text…
  expect(
    (await screen.findAllByText(new RegExp("onerror=window.__pwned"))).length,
  ).toBeGreaterThan(0);
  // …and never materialised as markup.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();

  // Decimal strings render byte-identically inside the KES format —
  // falsifiable: a numeric round-trip would drop the trailing ".10".
  expect(screen.getByText("KES 1,234,567.10")).toBeInTheDocument();
  expect(screen.getByText("KES 500,000.10")).toBeInTheDocument();
  expect(screen.getByText("KES 250,000.20")).toBeInTheDocument();
});

test("pledge queue: the stage filter drives the SERVER query over the PLEDGEABLE stages only; Load more follows the cursor VERBATIM (gate 1.3)", async () => {
  const user = userEvent.setup();
  mockedApps.fetchApplicationsPage
    .mockResolvedValueOnce(page([application()], "opaque-cursor-§1"))
    .mockResolvedValueOnce(page([application({ id: "dddddddd-1111-2222-3333-444444444444" })]));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mockedApps.fetchApplicationsPage).toHaveBeenCalledTimes(2));
  expect(mockedApps.fetchApplicationsPage.mock.calls[0]?.[0]).toEqual({ stage: "submitted" });
  expect(mockedApps.fetchApplicationsPage.mock.calls[0]?.[1]).toBeNull();
  expect(mockedApps.fetchApplicationsPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");

  // Only the three pledgeable stages are offered — there is no filter
  // for approved/disbursed/rejected (the UI never offers what the API
  // forbids) and no "all stages" escape hatch.
  const group = screen.getByRole("group", { name: "Application stage" });
  expect(within(group).getAllByRole("button")).toHaveLength(3);
  await user.click(within(group).getByRole("button", { name: "Appraisal" }));
  await waitFor(() =>
    expect(mockedApps.fetchApplicationsPage).toHaveBeenCalledWith({ stage: "appraisal" }, null),
  );
});

test("UI affordances follow the matrix: a view-only role gets NO pledge queue (zero queue fetches), NO session panel — aggregates still render", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  mountScreen();

  await screen.findByText("KES 1,234,567.10");
  expect(screen.queryByText("Open for pledging")).toBeNull();
  expect(screen.queryByText(/This session/)).toBeNull();
  // Structural: the stripped role never even FETCHES the queue or the
  // member picker.
  expect(mockedApps.fetchApplicationsPage).not.toHaveBeenCalled();
  expect(mockedMembers.fetchMembersPage).not.toHaveBeenCalled();
});

test("SELF-GUARANTEE structurally impossible: the borrower never appears among the guarantor options", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  const picker = within(drawer).getByLabelText("Guarantor");
  const options = within(picker).getAllByRole("option");
  const values = options.map((option) => (option as HTMLOptionElement).value);
  // The guarantor is offered; the borrower is NOT (no way to select them).
  expect(values).toContain(GUARANTOR_ID);
  expect(values).not.toContain(BORROWER_ID);
  // The server-reported free capacity for the chosen guarantor renders
  // verbatim from the fresh aggregate read.
  await user.selectOptions(picker, GUARANTOR_ID);
  expect(await within(drawer).findByText("KES 250,000.20")).toBeInTheDocument();
});

test("STALE-READ OVER-PLEDGE PREVENTION: opening the drawer re-reads the application AND the server aggregates fresh (record class, staleTime 0)", async () => {
  const user = userEvent.setup();
  mountScreen();

  // The screen strip fetched the composite once…
  await screen.findByText("KES 1,234,567.10");
  expect(mockedDashboard.fetchDashboardSummary).toHaveBeenCalledTimes(1);
  expect(mockedApps.fetchApplication).not.toHaveBeenCalled();

  await openPledgeDrawer(user);

  // …and the drawer NEVER trusts that cache before a money-adjacent
  // write: it re-reads the application record and re-fetches the
  // aggregates on mount (staleTime 0 + refetchOnMount always).
  expect(mockedApps.fetchApplication).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(mockedDashboard.fetchDashboardSummary).toHaveBeenCalledTimes(2));
});

test("DOUBLE-PLEDGE impossible from this client: triple-clicked confirmation produces exactly ONE write; the affordance is SPENT and the record enters the session panel", async () => {
  const user = userEvent.setup();
  let release: (value: Guarantee) => void = () => undefined;
  mocked.pledgeGuarantee.mockImplementation(
    () =>
      new Promise<Guarantee>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  const confirmButton = await confirmPledge(user, drawer);
  await user.click(confirmButton);
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);
  expect(mocked.pledgeGuarantee.mock.calls[0]?.[0]).toBe(APP_ID);
  // The amount travels as the typed decimal STRING — never a float.
  expect(mocked.pledgeGuarantee.mock.calls[0]?.[1]).toEqual({
    guarantor_member_id: GUARANTOR_ID,
    amount: "50000.10",
  });

  release(guarantee());

  // The result panel replaces the form — the affordance is spent…
  expect(await screen.findByText("Guarantee pledged")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Pledge…" })).toBeNull();
  // …the server's record renders verbatim and still exactly one write.
  expect(within(drawer).getByText("KES 50,000.10")).toBeInTheDocument();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);

  // The witnessed registry now carries the record in the session panel.
  await user.click(within(drawer).getByRole("button", { name: "Close" }));
  expect(await screen.findByText("Pledged (unconsented)")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Consent…" })).toBeInTheDocument();
});

test("stale pledge: 409 shows the explicit reload flow with EXACTLY ONE write attempt; reload fetches the moved stage, withdraws the action and never replays", async () => {
  const user = userEvent.setup();
  mocked.pledgeGuarantee.mockRejectedValue(new ApiError(409, "conflict", "corr-pledge"));
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  await user.click(await confirmPledge(user, drawer));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);

  // Explicit reload fetches the fresh record (approved underneath —
  // no longer pledgeable)…
  mockedApps.fetchApplication.mockResolvedValue(application({ stage: "approved", version: 3 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/no longer in a pledgeable stage/),
  ).toBeInTheDocument();
  // The reload notice reports a post-conflict state: informational
  // styling, NEVER the success variant — and it is announced (!60 F5).
  const reloadNotice = screen.getByText(/Record reloaded — re-check the stage and capacity/);
  expect(reloadNotice).toHaveClass("info");
  expect(reloadNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Record reloaded after the conflict — re-check the stage and capacity.",
  );
  // …the action is gone and the stale write was NEVER replayed.
  expect(within(drawer).queryByRole("button", { name: "Pledge…" })).toBeNull();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);
});

test("pledge idempotency keys: stable across retries of an identical intent, rotated when the amount changes", async () => {
  const user = userEvent.setup();
  mocked.pledgeGuarantee
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(guarantee());
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  // Attempt 1 fails with a 5xx…
  await user.click(await confirmPledge(user, drawer));
  await waitFor(() => expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1));

  // …identical retry (same guarantor, same amount) REUSES the key…
  await user.click(await confirmPledge(user, drawer));
  await waitFor(() => expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(2));
  const key1 = mocked.pledgeGuarantee.mock.calls[0]?.[2];
  const key2 = mocked.pledgeGuarantee.mock.calls[1]?.[2];
  expect(key1).toBe(key2);

  // …a different intent (new amount) ROTATES the key.
  await user.click(await confirmPledge(user, drawer, "60000.00"));
  await waitFor(() => expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(3));
  expect(mocked.pledgeGuarantee.mock.calls[2]?.[2]).not.toBe(key1);
});

test("pledge idempotency-key FRESHNESS: after a 409 + explicit reload (record version bumped) an IDENTICAL re-submission ROTATES the key — the pre-conflict key is never reused", async () => {
  const user = userEvent.setup();
  mocked.pledgeGuarantee
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-k1"))
    .mockResolvedValue(guarantee());
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  await user.click(await confirmPledge(user, drawer));
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);
  const preConflictKey = mocked.pledgeGuarantee.mock.calls[0]?.[2];

  // Concurrent activity moved the record (capacity freed by a release,
  // say): the stage is STILL pledgeable but the version bumped. The
  // explicit reload reads it fresh…
  mockedApps.fetchApplication.mockResolvedValue(application({ version: 3 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() => expect(mockedApps.fetchApplication).toHaveBeenCalledTimes(2));

  // …and re-submitting the IDENTICAL guarantor+amount is a NEW intent
  // against the reloaded record: the key ROTATES (reload-never-replay
  // does not rest on backend idempotency-store semantics).
  await user.click(await confirmPledge(user, drawer));
  await waitFor(() => expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(2));
  expect(mocked.pledgeGuarantee.mock.calls[1]?.[1]).toEqual(
    mocked.pledgeGuarantee.mock.calls[0]?.[1],
  );
  expect(mocked.pledgeGuarantee.mock.calls[1]?.[2]).not.toBe(preConflictKey);
});

test("pledge validation: client Zod verdicts render inline BEFORE any write; server 422 verdicts then WIN per field", async () => {
  const user = userEvent.setup();
  mocked.pledgeGuarantee.mockRejectedValue(
    new ApiError(422, "validation_error", null, [
      { field: "amount", message: "amount exceeds guarantor capacity policy" },
    ]),
  );
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  // Invalid client-side: no guarantor, malformed amount — zero writes.
  await user.type(within(drawer).getByLabelText("Pledge amount (KES)"), "12.345");
  await user.click(within(drawer).getByRole("button", { name: "Pledge…" }));
  expect(
    await within(drawer).findByText("Enter a positive amount (up to 2 decimal places)."),
  ).toBeInTheDocument();
  expect(within(drawer).getByText("Select the guarantor.")).toBeInTheDocument();
  expect(mocked.pledgeGuarantee).not.toHaveBeenCalled();

  // Fix the client issues; the server's field verdict renders inline.
  await user.click(await confirmPledge(user, drawer, "50000.00"));
  expect(
    await within(drawer).findByText("amount exceeds guarantor capacity policy"),
  ).toBeInTheDocument();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);
});

test("CONSENT: no write until the byte-identical phrase; double-clicked confirm posts ONCE; the server's status renders verbatim and the consent affordance is SPENT", async () => {
  const user = userEvent.setup();
  recordWitnessedGuarantee(guarantee());
  let release: (value: Guarantee) => void = () => undefined;
  mocked.consentGuarantee.mockImplementation(
    () =>
      new Promise<Guarantee>((resolve) => {
        release = resolve;
      }),
  );
  mockedMembers.fetchMember.mockImplementation((id: string) =>
    Promise.resolve(id === BORROWER_ID ? BORROWER : GUARANTOR),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Consent…" }));
  const dialog = await screen.findByRole("dialog", { name: "Record guarantor consent" });
  await within(dialog).findByText("KES 50,000.10");
  const armButton = within(dialog).getByRole("button", { name: "Record consent…" });
  // Consent converts the pledge into ACTIVE collateral (money-adjacent):
  // it carries the SAME danger treatment as pledge/release (!60 F7).
  expect(armButton).toHaveClass("danger");
  // P14.5: the staff consent override MUST cite its evidence — the arm
  // button stays disabled until the citation is typed, so a bare
  // consent shape cannot even be attempted from this screen.
  expect(armButton).toBeDisabled();
  await user.type(
    within(dialog).getByLabelText("Consent evidence reference"),
    "Signed consent form CF-14",
  );
  await user.click(armButton);

  const confirm = await screen.findByRole("dialog", { name: "Confirm consent attestation" });
  const confirmButton = within(confirm).getByRole("button", { name: "Record consent" });
  // Disabled until the phrase is typed byte-identically — zero writes.
  expect(confirmButton).toBeDisabled();
  expect(mocked.consentGuarantee).not.toHaveBeenCalled();

  const phrase = GUARANTEE_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.consentGuarantee).toHaveBeenCalledTimes(1);
  // The version the record was WITNESSED with pins the optimistic lock
  // and the citation travels as the third argument (P14.5).
  expect(mocked.consentGuarantee.mock.calls[0]?.[0]).toBe(GUARANTEE_ID);
  expect(mocked.consentGuarantee.mock.calls[0]?.[1]).toBe(1);
  expect(mocked.consentGuarantee.mock.calls[0]?.[2]).toBe("Signed consent form CF-14");

  release(guarantee({ status: "active", version: 2 }));

  // The server's word replaces the action (spent affordance)…
  expect(await within(dialog).findByText("Consent recorded")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Record consent…" })).toBeNull();
  expect(mocked.consentGuarantee).toHaveBeenCalledTimes(1);

  // …and the session panel now offers release (active, undisbursed) but
  // no second consent and no substitution (no loan behind it).
  await user.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(await screen.findByRole("button", { name: "Release…" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Consent…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Substitute…" })).toBeNull();
});

test("RELEASE 409 (record moved outside this tab): EXACTLY ONE attempt; reload structurally WITHDRAWS the record's affordances — replay impossible", async () => {
  const user = userEvent.setup();
  recordWitnessedGuarantee(guarantee({ status: "active" }));
  mocked.releaseGuarantee.mockRejectedValue(new ApiError(409, "conflict", "corr-release"));
  mockedMembers.fetchMember.mockImplementation((id: string) =>
    Promise.resolve(id === BORROWER_ID ? BORROWER : GUARANTOR),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Release…" }));
  const dialog = await screen.findByRole("dialog", { name: "Release guarantee" });
  await user.click(within(dialog).getByRole("button", { name: "Release…" }));
  const confirm = await screen.findByRole("dialog", { name: "Confirm release" });
  const phrase = GUARANTEE_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm).getByRole("button", { name: "Release guarantee" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.releaseGuarantee).toHaveBeenCalledTimes(1);
  // The single attempt carried the witnessed version.
  expect(mocked.releaseGuarantee.mock.calls[0]?.[1]).toBe(1);

  // Explicit reload: the contract exposes NO per-guarantee read, so the
  // tab's stale copy is structurally withdrawn instead — its actions are
  // gone from the dialog AND the session panel; nothing was replayed.
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  const withdrawalNotice = await screen.findByText(/actions are withdrawn here/);
  // The notice reports a CONFLICTED record: informational styling, NEVER
  // the success variant — and the withdrawal is announced (!60 F5).
  expect(withdrawalNotice).toHaveClass("info");
  expect(withdrawalNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Guarantee actions withdrawn — the record changed outside this tab.",
  );
  await user.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(await screen.findByText(/Changed outside this tab/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Release…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Consent…" })).toBeNull();
  expect(mocked.releaseGuarantee).toHaveBeenCalledTimes(1);
});

test("SUBSTITUTION (disbursed collateral): release is NOT offered; the swap requires attestation + reference; ONE write records BOTH server records verbatim", async () => {
  const user = userEvent.setup();
  recordWitnessedGuarantee(guarantee({ status: "active", loan_id: LOAN_ID }));
  const swap: Substitution = {
    released: guarantee({ status: "released", loan_id: LOAN_ID, version: 2 }),
    replacement: guarantee({
      id: "eeeeeeee-1111-2222-3333-444444444444",
      status: "active",
      loan_id: LOAN_ID,
      amount: "50000.10",
      version: 1,
    }),
  };
  mocked.substituteGuarantee.mockResolvedValue(swap);
  mockedMembers.fetchMember.mockImplementation((id: string) =>
    Promise.resolve(id === BORROWER_ID ? BORROWER : GUARANTOR),
  );
  mountScreen();

  // An active guarantee behind a DISBURSED loan can only be substituted.
  expect(await screen.findByRole("button", { name: "Substitute…" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Release…" })).toBeNull();

  await user.click(screen.getByRole("button", { name: "Substitute…" }));
  const drawer = await screen.findByRole("dialog", { name: "Substitute guarantee" });

  // Unreferenced submissions never reach the wire (P14.5: the evidence
  // citation IS the attestation — no consented checkbox exists to tick).
  await user.selectOptions(
    within(drawer).getByLabelText("Substitute guarantor"),
    GUARANTOR_ID,
  );
  await user.click(within(drawer).getByRole("button", { name: "Substitute…" }));
  expect(
    await within(drawer).findByText(
      "Cite the consent evidence (e.g. the signed guarantorship form).",
    ),
  ).toBeInTheDocument();
  expect(within(drawer).queryByLabelText("Consent attestation")).toBeNull();
  expect(mocked.substituteGuarantee).not.toHaveBeenCalled();

  await user.type(
    within(drawer).getByLabelText("Consent evidence reference"),
    "Signed guarantorship form GF-778",
  );
  await user.click(within(drawer).getByRole("button", { name: "Substitute…" }));
  const confirm = await screen.findByRole("dialog", { name: "Confirm substitution" });
  const phrase = GUARANTEE_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  const confirmButton = within(confirm).getByRole("button", { name: "Substitute guarantee" });
  await user.click(confirmButton);
  await user.click(confirmButton);

  await waitFor(() => expect(mocked.substituteGuarantee).toHaveBeenCalledTimes(1));
  expect(mocked.substituteGuarantee.mock.calls[0]?.[0]).toBe(GUARANTEE_ID);
  expect(mocked.substituteGuarantee.mock.calls[0]?.[1]).toBe(1);
  // Blank replacement amount ⇒ null (the API layer omits it and the
  // server derives the floor from the guarantee row).
  expect(mocked.substituteGuarantee.mock.calls[0]?.[2]).toEqual({
    guarantor_member_id: GUARANTOR_ID,
    consent_reference: "Signed guarantorship form GF-778",
    amount: null,
  });

  // Both sides of the atomic swap render verbatim (spent affordance).
  expect(
    await within(drawer).findByText("Substitution recorded (atomic swap)"),
  ).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Substitute…" })).toBeNull();
});

test("SUBSTITUTE 409: reload withdraws the record with an INFORMATIONAL (never success) notice and ANNOUNCES the withdrawal; the stale swap is never replayed", async () => {
  const user = userEvent.setup();
  recordWitnessedGuarantee(guarantee({ status: "active", loan_id: LOAN_ID }));
  mocked.substituteGuarantee.mockRejectedValue(new ApiError(409, "conflict", "corr-sub"));
  mockedMembers.fetchMember.mockImplementation((id: string) =>
    Promise.resolve(id === BORROWER_ID ? BORROWER : GUARANTOR),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Substitute…" }));
  const drawer = await screen.findByRole("dialog", { name: "Substitute guarantee" });
  await user.selectOptions(
    within(drawer).getByLabelText("Substitute guarantor"),
    GUARANTOR_ID,
  );
  await user.type(
    within(drawer).getByLabelText("Consent evidence reference"),
    "Signed guarantorship form GF-778",
  );
  await user.click(within(drawer).getByRole("button", { name: "Substitute…" }));
  const confirm = await screen.findByRole("dialog", { name: "Confirm substitution" });
  const phrase = GUARANTEE_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm).getByRole("button", { name: "Substitute guarantee" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.substituteGuarantee).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  const withdrawalNotice = await screen.findByText(/actions are withdrawn here/);
  expect(withdrawalNotice).toHaveClass("info");
  expect(withdrawalNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Guarantee actions withdrawn — the record changed outside this tab.",
  );
  // The form is structurally withdrawn and the stale swap NEVER replayed.
  expect(within(drawer).queryByRole("button", { name: "Substitute…" })).toBeNull();
  expect(mocked.substituteGuarantee).toHaveBeenCalledTimes(1);
});

test("least-disclosure: a 403 on the pledge renders the sanitized banner only, session intact", async () => {
  const user = userEvent.setup();
  mocked.pledgeGuarantee.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  const drawer = await openPledgeDrawer(user);
  await user.click(await confirmPledge(user, drawer));

  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.pledgeGuarantee).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("query-path 401 tears the session down immediately (dual-cache teardown) AND empties the witnessed registry — the session panel renders nothing", async () => {
  // A previous operator's witnessed record is armed in this tab…
  recordWitnessedGuarantee(guarantee());
  mockedDashboard.fetchDashboardSummary.mockRejectedValue(
    new ApiError(401, "unauthenticated", "corr-q"),
  );
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });

  // …and dies WITH the session (!60 F2): no witnessed financial record,
  // no armed affordance survives into the next operator's session.
  await waitFor(() =>
    expect(screen.queryByText("Pledged (unconsented)")).toBeNull(),
  );
  expect(screen.queryByText("KES 50,000.10")).toBeNull();
  expect(screen.queryByRole("button", { name: "Consent…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Release…" })).toBeNull();
  expect(
    screen.getByText(/No guarantees recorded in this session yet/),
  ).toBeInTheDocument();
});

test("explicit sign-out empties the witnessed registry — the session panel renders nothing for the next operator", async () => {
  recordWitnessedGuarantee(guarantee());
  mountScreen();

  // The record and its armed affordance are on screen pre-sign-out.
  expect(await screen.findByText("Pledged (unconsented)")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Consent…" })).toBeInTheDocument();

  // Real sign-out path: local teardown must win even if the revocation
  // call cannot complete in this harness.
  await act(async () => {
    await logout().catch(() => undefined);
  });

  expect(hasSession()).toBe(false);
  expect(screen.queryByText("Pledged (unconsented)")).toBeNull();
  expect(screen.queryByText("KES 50,000.10")).toBeNull();
  expect(screen.queryByRole("button", { name: "Consent…" })).toBeNull();
  expect(
    screen.getByText(/No guarantees recorded in this session yet/),
  ).toBeInTheDocument();
});

test("Zod boundary rejects money as NUMBERS and unknown statuses; both write boundaries STRIP extra keys", () => {
  expect(guaranteeSchema.safeParse(guarantee()).success).toBe(true);
  expect(guaranteeSchema.safeParse({ ...guarantee(), amount: 50000.1 }).success).toBe(false);
  expect(guaranteeSchema.safeParse({ ...guarantee(), status: "suspended" }).success).toBe(false);

  // Anything beyond the contract (internal ledger fields, say) can never
  // reach the DOM: the boundaries strip it.
  const parsed = guaranteeSchema.parse({
    ...guarantee(),
    internal_gl_account: "GL-SECRET-01",
  });
  expect("internal_gl_account" in parsed).toBe(false);

  const swap = substitutionSchema.parse({
    released: { ...guarantee(), audit_trail: ["secret"] },
    replacement: guarantee(),
  });
  expect("audit_trail" in swap.released).toBe(false);
});

test("money-input hygiene: leading zeros are rejected as a PURE STRING shape (no numeric coercion) — '007.10' can never reach fmtKes in the confirmation banner", () => {
  const pledgeAmountOk = (amount: string) =>
    pledgeCreateSchema.safeParse({ guarantor_member_id: GUARANTOR_ID, amount }).success;
  const substituteAmountOk = (amount: string) =>
    substituteCreateSchema.safeParse({
      guarantor_member_id: GUARANTOR_ID,
      consent_reference: "Signed guarantorship form GF-778",
      amount,
    }).success;

  // Canonical decimal strings pass…
  expect(pledgeAmountOk("50000.10")).toBe(true);
  expect(pledgeAmountOk("0.50")).toBe(true);
  expect(substituteAmountOk("60000.10")).toBe(true);
  // …leading zeros are refused at the boundary (both money inputs)…
  expect(pledgeAmountOk("007.10")).toBe(false);
  expect(pledgeAmountOk("00")).toBe(false);
  expect(pledgeAmountOk("01")).toBe(false);
  expect(substituteAmountOk("007.10")).toBe(false);
  // …and the existing non-zero refine still rejects all-zero values.
  expect(pledgeAmountOk("0")).toBe(false);
  expect(pledgeAmountOk("0.00")).toBe(false);
});
