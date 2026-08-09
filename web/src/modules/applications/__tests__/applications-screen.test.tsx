/**
 * Applications register + intake + detail adversarial tests THROUGH THE
 * REAL SCREEN CODE (P15 Applications/Committee batch), mirroring the
 * users/settings reference suites: the feature API layer is stubbed at
 * the module boundary; rendering, keyset paging, permission gating,
 * idempotency-key custody, 409 handling and the typed-confirmation
 * terminal reject are the real shipped code, mounted under the REAL
 * <Providers>.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { announce } from "@/modules/layout/announcer";
import { ApplicationsScreen } from "../components/ApplicationsScreen";
import { clearCommitteeMakers } from "../makerRegistry";
import {
  applicationCreateSchema,
  applicationSchema,
  type Application,
  type Product,
} from "../schemas";
import * as appsApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchApplicationsPage: jest.fn(),
    fetchApplication: jest.fn(),
    fetchProducts: jest.fn(),
    createApplication: jest.fn(),
    transitionApplication: jest.fn(),
    voteOnApplication: jest.fn(),
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
// async outcome — including the post-conflict reload (W59-4) — is
// announced.
jest.mock("@/modules/layout/announcer", () => {
  const actual = jest.requireActual<typeof import("@/modules/layout/announcer")>(
    "@/modules/layout/announcer",
  );
  return { ...actual, announce: jest.fn() };
});

const mocked = jest.mocked(appsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);
const mockedAnnounce = jest.mocked(announce);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";

// Attacker-influenced free-text field on this screen — must render as
// inert text (named XSS threat).
const HOSTILE_PURPOSE = '<img src=x onerror=window.__pwned=1> "quoted" purpose';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function baseApplication(overrides: Partial<Application> = {}): Application {
  return {
    id: APP_ID,
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
    rate_pct: "12.50",
    purpose: "School fees",
    stage: "submitted",
    cover_pct: "120.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 3,
    ...overrides,
  };
}

const PRODUCT: Product = {
  id: PRODUCT_ID,
  name: "Development Loan",
  rate_pct: "12.50",
  deposit_multiplier: "3.00",
  max_term_months: 48,
  guarantors_required: 2,
  active: true,
  version: 1,
};

const MEMBER = {
  id: MEMBER_ID,
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

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "applications",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "applications",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function page(items: Application[], nextCursor: string | null = null) {
  return { items, nextCursor };
}

function mountScreen() {
  return render(
    <Providers>
      <ApplicationsScreen />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  clearCommitteeMakers();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchApplicationsPage.mockResolvedValue(page([baseApplication()]));
  mocked.fetchApplication.mockResolvedValue(baseApplication());
  mocked.fetchProducts.mockResolvedValue([PRODUCT]);
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
  mockedMembers.fetchMembersPage.mockResolvedValue({
    items: [MEMBER],
    nextCursor: null,
  });
});

afterEach(() => {
  clearSession();
});

test("hostile purpose renders as inert TEXT; money figures render VERBATIM through fmtKes (named XSS threat + blocker (a))", async () => {
  mocked.fetchApplicationsPage.mockResolvedValue(
    page([baseApplication({ purpose: HOSTILE_PURPOSE })]),
  );
  const { container } = mountScreen();

  // The payload is visible as literal text…
  expect(await screen.findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  // …and never materialised as markup.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();

  // Decimal strings render byte-identically inside the KES format —
  // falsifiable: a numeric round-trip would drop the trailing ".10".
  expect(screen.getByText("KES 250,000.10")).toBeInTheDocument();
  expect(screen.getByText("120.00%")).toBeInTheDocument();
});

test("keyset paging: Load more follows the server cursor VERBATIM — no offset anywhere (gate 1.3)", async () => {
  const user = userEvent.setup();
  mocked.fetchApplicationsPage
    .mockResolvedValueOnce(page([baseApplication()], "opaque-cursor-§1"))
    .mockResolvedValueOnce(page([baseApplication({ id: "bbbbbbbb-1111-2222-3333-444444444444" })]));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));

  await waitFor(() => expect(mocked.fetchApplicationsPage).toHaveBeenCalledTimes(2));
  // First page: null cursor; second: the opaque server cursor untouched.
  expect(mocked.fetchApplicationsPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchApplicationsPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");
});

test("stage filter drives the SERVER query — the client never filters locally", async () => {
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 250,000.10");
  await user.click(screen.getByRole("button", { name: "Committee" }));

  await waitFor(() =>
    expect(mocked.fetchApplicationsPage).toHaveBeenCalledWith(
      { stage: "committee" },
      null,
    ),
  );
});

test("UI affordances follow the matrix: view-only role sees NO intake button and NO officer actions", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 250,000.10");
  expect(screen.queryByRole("button", { name: "+ New application" })).toBeNull();

  // Drill into the detail drawer: no stage-move affordances mount.
  await user.click(screen.getByText("KES 250,000.10"));
  await screen.findByText("Record version");
  expect(screen.queryByText("Officer action")).toBeNull();
  expect(screen.queryByRole("button", { name: /Move to appraisal/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Reject…/ })).toBeNull();
});

test("intake double-submit produces exactly ONE create (pending short-circuit); the amount travels as the typed STRING", async () => {
  const user = userEvent.setup();
  let release: (value: Application) => void = () => undefined;
  mocked.createApplication.mockImplementation(
    () =>
      new Promise<Application>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ New application" }));
  const dialog = await screen.findByRole("dialog", { name: "New loan application" });
  await user.selectOptions(within(dialog).getByLabelText("Member"), MEMBER_ID);
  await user.selectOptions(within(dialog).getByLabelText("Loan product"), PRODUCT_ID);
  await user.type(within(dialog).getByLabelText("Amount (KES)"), "250000.10");
  await user.type(within(dialog).getByLabelText("Term (months)"), "24");

  const submit = within(dialog).getByRole("button", { name: "Submit application" });
  await user.click(submit);
  await user.click(submit);
  await user.click(submit);

  expect(mocked.createApplication).toHaveBeenCalledTimes(1);
  // Decimal string end-to-end — never parsed into a float.
  expect(mocked.createApplication.mock.calls[0]?.[0]).toMatchObject({
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
  });
  release(baseApplication());
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "New loan application" })).toBeNull());
});

test("intake: client Zod verdicts render inline BEFORE any write; server 422 verdicts then WIN per field", async () => {
  const user = userEvent.setup();
  mocked.createApplication.mockRejectedValue(
    new ApiError(422, "validation_error", null, [
      { field: "amount", message: "amount exceeds member eligibility" },
    ]),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ New application" }));
  const dialog = await screen.findByRole("dialog", { name: "New loan application" });

  // Invalid client-side: no member, malformed amount — zero writes.
  await user.type(within(dialog).getByLabelText("Amount (KES)"), "12.345");
  await user.click(within(dialog).getByRole("button", { name: "Submit application" }));
  expect(await within(dialog).findByText("Select a member.")).toBeInTheDocument();
  expect(
    within(dialog).getByText("Enter a positive amount (up to 2 decimal places)."),
  ).toBeInTheDocument();
  expect(mocked.createApplication).not.toHaveBeenCalled();

  // Fix the client issues; the server's field verdict renders inline.
  await user.selectOptions(within(dialog).getByLabelText("Member"), MEMBER_ID);
  await user.selectOptions(within(dialog).getByLabelText("Loan product"), PRODUCT_ID);
  await user.clear(within(dialog).getByLabelText("Amount (KES)"));
  await user.type(within(dialog).getByLabelText("Amount (KES)"), "250000");
  await user.type(within(dialog).getByLabelText("Term (months)"), "24");
  await user.click(within(dialog).getByRole("button", { name: "Submit application" }));

  expect(await within(dialog).findByText("amount exceeds member eligibility")).toBeInTheDocument();
  expect(mocked.createApplication).toHaveBeenCalledTimes(1);
});

test("stale stage move: 409 shows the explicit reload flow with EXACTLY ONE write attempt; reload never replays", async () => {
  const user = userEvent.setup();
  mocked.transitionApplication.mockRejectedValue(new ApiError(409, "conflict", "corr-stale"));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await user.click(await screen.findByRole("button", { name: "Move to appraisal" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.transitionApplication).toHaveBeenCalledTimes(1);
  expect(mocked.transitionApplication.mock.calls[0]?.[1]).toMatchObject({
    target: "appraisal",
    version: 3,
  });

  // Explicit reload fetches the fresh record (stage moved server-side)…
  mocked.fetchApplication.mockResolvedValue(baseApplication({ stage: "appraisal", version: 4 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(await screen.findByRole("button", { name: "Recommend to committee" })).toBeInTheDocument();
  // …and NEVER replays the stale submission.
  expect(mocked.transitionApplication).toHaveBeenCalledTimes(1);
  // The reload notice reports a post-conflict state: informational
  // styling, NEVER the success variant — and it is announced (W59-4).
  const reloadNotice = screen.getByText(/Record reloaded — re-check the stage/);
  expect(reloadNotice).toHaveClass("info");
  expect(reloadNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Record reloaded after the conflict — re-check the stage.",
  );
});

test("terminal reject flows through the typed confirmation: NO write until the byte-identical phrase is typed", async () => {
  const user = userEvent.setup();
  mocked.transitionApplication.mockResolvedValue(baseApplication({ stage: "rejected", version: 4 }));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await user.click(await screen.findByRole("button", { name: "Reject…" }));

  const dialog = await screen.findByRole("dialog", { name: "Reject application" });
  const confirmButton = within(dialog).getByRole("button", { name: "Reject application" });
  expect(confirmButton).toBeDisabled();

  const phrase = APP_ID.slice(0, 8);
  await user.type(within(dialog).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  expect(confirmButton).toBeEnabled();
  await user.click(confirmButton);

  await waitFor(() => expect(mocked.transitionApplication).toHaveBeenCalledTimes(1));
  expect(mocked.transitionApplication.mock.calls[0]?.[1]).toMatchObject({
    target: "rejected",
    version: 3,
  });
});

test("idempotency keys: stable across retries of an identical move, rotated when the intent changes", async () => {
  const user = userEvent.setup();
  mocked.transitionApplication
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockResolvedValue(baseApplication({ stage: "appraisal", version: 4 }));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await user.click(await screen.findByRole("button", { name: "Move to appraisal" }));
  await waitFor(() => expect(mocked.transitionApplication).toHaveBeenCalledTimes(1));

  // Identical retry (same target, same version) REUSES the key.
  await user.click(screen.getByRole("button", { name: "Move to appraisal" }));
  await waitFor(() => expect(mocked.transitionApplication).toHaveBeenCalledTimes(2));
  const key1 = mocked.transitionApplication.mock.calls[0]?.[2];
  const key2 = mocked.transitionApplication.mock.calls[1]?.[2];
  expect(key1).toBe(key2);

  // A different intent (new version in the body) ROTATES the key.
  await screen.findByRole("button", { name: "Recommend to committee" });
  await user.click(screen.getByRole("button", { name: "Recommend to committee" }));
  await waitFor(() => expect(mocked.transitionApplication).toHaveBeenCalledTimes(3));
  expect(mocked.transitionApplication.mock.calls[2]?.[2]).not.toBe(key1);
});

test("least-disclosure: a 403 renders the sanitized banner only, no reload affordance, session intact", async () => {
  const user = userEvent.setup();
  mocked.transitionApplication.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await user.click(await screen.findByRole("button", { name: "Move to appraisal" }));

  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.transitionApplication).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("query-path 401 tears the session down immediately (dual-cache teardown)", async () => {
  mocked.fetchApplicationsPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("Zod boundary rejects money as NUMBERS and unknown stages — contract violations never reach the DOM", () => {
  expect(applicationSchema.safeParse(baseApplication()).success).toBe(true);
  expect(
    applicationSchema.safeParse({ ...baseApplication(), amount: 250000.1 }).success,
  ).toBe(false);
  expect(
    applicationSchema.safeParse({ ...baseApplication(), cover_pct: 120 }).success,
  ).toBe(false);
  expect(
    applicationSchema.safeParse({ ...baseApplication(), stage: "fast_tracked" }).success,
  ).toBe(false);
});

test("attribution (issue #30 / !66 follow-up): the detail drawer renders created_by as the SHORT id with the full UUID on title — and NEVER fetches a directory record for it (least disclosure, FM-D)", async () => {
  const CREATOR_ID = "66666666-6666-6666-6666-666666666666";
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(baseApplication({ created_by: CREATOR_ID }));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  // Short-id convention: the 8-char prefix renders; the full UUID rides
  // the title attribute (same treatment as every other opaque id).
  const cell = screen.getByTitle(CREATOR_ID);
  expect(cell).toHaveTextContent(CREATOR_ID.slice(0, 8));
  // Least disclosure: the staff UUID is NEVER resolved to a person —
  // the only member fetch is the applicant's, and no name/email for
  // the initiator appears anywhere in the DOM.
  for (const call of mockedMembers.fetchMember.mock.calls) {
    expect(call[0]).not.toBe(CREATOR_ID);
  }
  expect(screen.queryByText(/full_name/)).toBeNull();
});

test("attribution NULL leg (FM-B): an unattributed record renders the honest affordance — never an invented actor", async () => {
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(baseApplication({ created_by: null }));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  expect(screen.getByText("— (unattributed)")).toBeInTheDocument();
});

test("attribution XSS inertness: a hostile created_by string renders as inert TEXT in the UUID cell — no markup materialises", async () => {
  const HOSTILE_CREATOR = '<img src=x onerror=window.__pwned=3>';
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(baseApplication({ created_by: HOSTILE_CREATOR }));
  const { container } = mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  // The sliced 8-char prefix ("<img src") renders as literal text…
  expect(screen.getByTitle(HOSTILE_CREATOR)).toHaveTextContent("<img src");
  // …and never as an element.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("recommender attribution (issue #30 close-out, 0037): the detail drawer renders recommended_by as the SHORT id with the full UUID on title — and NEVER fetches a directory record for it (least disclosure, FM-D)", async () => {
  const RECOMMENDER_ID = "77777777-7777-7777-7777-777777777777";
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(
    baseApplication({ stage: "committee", recommended_by: RECOMMENDER_ID }),
  );
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  // Short-id convention: the 8-char prefix renders; the full UUID rides
  // the title attribute (same treatment as created_by).
  const cell = screen.getByTitle(RECOMMENDER_ID);
  expect(cell).toHaveTextContent(RECOMMENDER_ID.slice(0, 8));
  // Least disclosure: the staff UUID is NEVER resolved to a person.
  for (const call of mockedMembers.fetchMember.mock.calls) {
    expect(call[0]).not.toBe(RECOMMENDER_ID);
  }
  expect(screen.queryByText(/full_name/)).toBeNull();
});

test("recommender NULL leg (FM-B): a not-yet-referred / unattributed record renders the honest affordance — never an invented actor", async () => {
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(baseApplication({ recommended_by: null }));
  mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  expect(screen.getByText("— (not referred / unattributed)")).toBeInTheDocument();
});

test("recommender XSS inertness: a hostile recommended_by string renders as inert TEXT in the UUID cell — no markup materialises", async () => {
  const HOSTILE_RECOMMENDER = '<img src=x onerror=window.__pwned=4>';
  const user = userEvent.setup();
  mocked.fetchApplication.mockResolvedValue(
    baseApplication({ recommended_by: HOSTILE_RECOMMENDER }),
  );
  const { container } = mountScreen();

  await user.click(await screen.findByText("KES 250,000.10"));
  await screen.findByText("Record version");
  // The sliced 8-char prefix ("<img src") renders as literal text…
  expect(screen.getByTitle(HOSTILE_RECOMMENDER)).toHaveTextContent("<img src");
  // …and never as an element.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("intake amount rejects leading zeros and zero amounts (W58-5, the !60 F6 class) — pure string shape", () => {
  const base = {
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    term_months: 24,
    purpose: null,
  };
  const parse = (amount: string) => applicationCreateSchema.safeParse({ ...base, amount });
  // Well-formed decimal strings pass…
  expect(parse("250000").success).toBe(true);
  expect(parse("250000.10").success).toBe(true);
  expect(parse("0.50").success).toBe(true);
  // …leading zeros are rejected (never rendered as "KES 007.10")…
  expect(parse("007.10").success).toBe(false);
  expect(parse("012").success).toBe(false);
  expect(parse("00.10").success).toBe(false);
  // …and zero is not a loan amount.
  expect(parse("0").success).toBe(false);
  expect(parse("0.00").success).toBe(false);
});
