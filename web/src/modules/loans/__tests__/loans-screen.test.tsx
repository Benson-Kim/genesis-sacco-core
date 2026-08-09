/**
 * Loan-book register + detail + disbursement + repayment adversarial
 * tests THROUGH THE REAL SCREEN CODE (P15 Loan book batch), mirroring
 * the applications/users reference suites: the feature API layers are
 * stubbed at the module boundary; rendering, keyset paging, permission
 * gating, idempotency-key custody, 409 handling and the typed
 * confirmations are the real shipped code, mounted under the REAL
 * <Providers>.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { announce } from "@/modules/layout/announcer";
import { LoansScreen } from "../components/LoansScreen";
import {
  disbursementSchema,
  loanSchema,
  portfolioSummarySchema,
  repaymentCreateSchema,
  type Disbursement,
  type Loan,
  type PortfolioSummary,
  type RepaymentResult,
} from "../schemas";
import type { Application } from "@/modules/applications/schemas";
import * as loansApi from "../api";
import * as appsApi from "@/modules/applications/api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchLoansPage: jest.fn(),
    fetchLoan: jest.fn(),
    fetchLoanSchedule: jest.fn(),
    fetchSettlementQuote: jest.fn(),
    fetchPortfolioSummary: jest.fn(),
    disburseApplication: jest.fn(),
    recordRepayment: jest.fn(),
  };
});

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
// async outcome — including the post-conflict reload (W59-4) — is
// announced.
jest.mock("@/modules/layout/announcer", () => {
  const actual = jest.requireActual<typeof import("@/modules/layout/announcer")>(
    "@/modules/layout/announcer",
  );
  return { ...actual, announce: jest.fn() };
});

const mocked = jest.mocked(loansApi);
const mockedApps = jest.mocked(appsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);
const mockedAnnounce = jest.mocked(announce);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const PRODUCT_ID = "44444444-4444-4444-4444-444444444444";
const APP_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const LOAN_ID = "cccccccc-1111-2222-3333-444444444444";

// Attacker-influenced free-text fields on this screen — must render as
// inert text (named XSS threat).
const HOSTILE_NAME = '<img src=x onerror=window.__pwned=1> "quoted" product';
const HOSTILE_REF = "<script>window.__pwned=2</script>TXN-REF";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function baseLoan(overrides: Partial<Loan> = {}): Loan {
  return {
    id: LOAN_ID,
    application_id: APP_ID,
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    principal: "2000000.00",
    balance: "1234567.10",
    rate_pct: "12.50",
    term_months: 36,
    status: "active",
    classification: "substandard",
    days_past_due: 45,
    provision_pct: "25.00",
    penalty_due: "1500.10",
    disbursed_at: "2026-01-15T09:00:00Z",
    closed_at: null,
    version: 7,
    ...overrides,
  };
}

const SCHEDULE_ROW = {
  installment_no: 1,
  due_date: "2026-02-15",
  principal_due: "50000.10",
  interest_due: "20833.33",
  total_due: "70833.43",
  paid_amount: "0.00",
};

const SUMMARY: PortfolioSummary = {
  active_loans: 12,
  outstanding_balance: "9876543.10",
  npl_balance: "1111111.10",
  npl_ratio_pct: "12.50",
  par30_balance: "2000000.00",
  par30_ratio_pct: "20.25",
  provisions: "456789.10",
  penalties_due: "9999.10",
  by_classification: [
    { classification: "normal", count: 9, balance: "7000000.00", provisions: "70000.00" },
    { classification: "substandard", count: 3, balance: "2876543.10", provisions: "386789.10" },
  ],
};

function approvedApplication(overrides: Partial<Application> = {}): Application {
  return {
    id: APP_ID,
    member_id: MEMBER_ID,
    product_id: PRODUCT_ID,
    amount: "250000.10",
    term_months: 24,
    rate_pct: "12.50",
    purpose: "School fees",
    stage: "approved",
    cover_pct: "120.00",
    created_by: null,
    recommended_by: null,
    max_eligible: "300000.00",
    version: 5,
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
    { module: "loan_book", can_view: true, can_create: true, can_edit: true, can_approve: true },
    { module: "applications", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "transactions", can_view: true, can_create: true, can_edit: false, can_approve: false },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "loan_book", can_view: true, can_create: false, can_edit: false, can_approve: false },
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
      <LoansScreen />
    </Providers>,
  );
}

/** Open the disbursement dialog and drive it through the typed
 * confirmation (shared by the double-submit / 409 / key-custody tests). */
async function openDisburseDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Disburse ›"));
  const dialog = await screen.findByRole("dialog", { name: "Disburse loan" });
  await within(dialog).findByText("KES 250,000.10");
  return dialog;
}

async function confirmDisburse(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
  channel = "mpesa",
) {
  await user.selectOptions(within(dialog).getByLabelText("Disbursement channel"), channel);
  await user.click(within(dialog).getByRole("button", { name: "Disburse…" }));
  const confirm = await screen.findByRole("dialog", { name: "Disburse funds" });
  const phrase = APP_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  return within(confirm).getByRole("button", { name: "Disburse funds" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchLoansPage.mockResolvedValue(page([baseLoan()]));
  mocked.fetchLoan.mockResolvedValue(baseLoan());
  mocked.fetchLoanSchedule.mockResolvedValue([SCHEDULE_ROW]);
  mocked.fetchSettlementQuote.mockResolvedValue({
    as_of: "2026-08-03",
    principal_balance: "1234567.10",
    interest_due: "10000.10",
    penalties_due: "1500.10",
    total: "1246067.30",
  });
  mocked.fetchPortfolioSummary.mockResolvedValue(SUMMARY);
  mockedApps.fetchApplicationsPage.mockResolvedValue(page([approvedApplication()]));
  mockedApps.fetchApplication.mockResolvedValue(approvedApplication());
  mockedApps.fetchProducts.mockResolvedValue([PRODUCT]);
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
});

afterEach(() => {
  clearSession();
});

test("hostile product name renders as inert TEXT; register + summary money renders VERBATIM through fmtKes (named XSS threat + blocker (a))", async () => {
  mockedApps.fetchProducts.mockResolvedValue([{ ...PRODUCT, name: HOSTILE_NAME }]);
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
  expect(screen.getByText("KES 9,876,543.10")).toBeInTheDocument();
  expect(screen.getByText("KES 1,500.10")).toBeInTheDocument();
  // Ratio figures are server strings rendered verbatim.
  expect(screen.getByText("12.50% of book")).toBeInTheDocument();
  expect(screen.getByText("20.25% of book")).toBeInTheDocument();
});

test("keyset paging: Load more follows the server cursor VERBATIM — no offset anywhere (gate 1.3)", async () => {
  const user = userEvent.setup();
  mocked.fetchLoansPage
    .mockResolvedValueOnce(page([baseLoan()], "opaque-cursor-§1"))
    .mockResolvedValueOnce(page([baseLoan({ id: "dddddddd-1111-2222-3333-444444444444" })]));
  // Keep the queue's own Load more out of the register assertion.
  mockedApps.fetchApplicationsPage.mockResolvedValue(page([]));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));

  await waitFor(() => expect(mocked.fetchLoansPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchLoansPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchLoansPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");
});

test("status + classification filters drive the SERVER query — the client never filters locally", async () => {
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 1,234,567.10");
  await user.click(screen.getByRole("button", { name: "Written off" }));
  await waitFor(() =>
    expect(mocked.fetchLoansPage).toHaveBeenCalledWith(
      { status: "written_off", classification: "" },
      null,
    ),
  );

  await user.click(screen.getByRole("button", { name: "Doubtful" }));
  await waitFor(() =>
    expect(mocked.fetchLoansPage).toHaveBeenCalledWith(
      { status: "written_off", classification: "doubtful" },
      null,
    ),
  );
});

test("UI affordances follow the matrix: a view-only role gets NO disbursement queue (zero queue fetches) and NO repayment form", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 1,234,567.10");
  expect(screen.queryByText("Disbursement queue")).toBeNull();
  // Structural: the stripped role never even FETCHES the approved queue.
  expect(mockedApps.fetchApplicationsPage).not.toHaveBeenCalled();

  // Drill into the loan drawer: no repayment affordances mount
  // (transactions:create is absent from the matrix).
  await user.click(screen.getByText("KES 1,234,567.10"));
  await screen.findByText("Record version");
  expect(screen.queryByText("Record repayment")).toBeNull();
  expect(screen.queryByRole("button", { name: /Record repayment/ })).toBeNull();
});

test("DOUBLE-DISBURSEMENT impossible from this client: triple-clicked confirmation produces exactly ONE write; the affordance is SPENT after success", async () => {
  const user = userEvent.setup();
  let release: (value: Disbursement) => void = () => undefined;
  mocked.disburseApplication.mockImplementation(
    () =>
      new Promise<Disbursement>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  const dialog = await openDisburseDialog(user);
  const confirmButton = await confirmDisburse(user, dialog);
  await user.click(confirmButton);
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);
  expect(mocked.disburseApplication.mock.calls[0]?.[0]).toBe(APP_ID);
  expect(mocked.disburseApplication.mock.calls[0]?.[1]).toBe("mpesa");

  release({
    loan_id: LOAN_ID,
    txn_id: "eeeeeeee-1111-2222-3333-444444444444",
    txn_ref: HOSTILE_REF,
    schedule: [SCHEDULE_ROW],
  });

  // The result panel replaces the action — the affordance is spent…
  expect(await screen.findByText(new RegExp("TXN-REF"))).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Disburse…" })).toBeNull();
  // …the hostile txn ref stayed inert TEXT (no script materialised)…
  expect(document.body.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
  // …the response schedule rendered verbatim; still exactly one write.
  expect(screen.getByText("KES 70,833.43")).toBeInTheDocument();
  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);
});

test("stale disbursement: 409 shows the explicit reload flow with EXACTLY ONE write attempt; reload fetches the moved stage and never replays", async () => {
  const user = userEvent.setup();
  mocked.disburseApplication.mockRejectedValue(new ApiError(409, "conflict", "corr-disb"));
  mountScreen();

  const dialog = await openDisburseDialog(user);
  const confirmButton = await confirmDisburse(user, dialog);
  await user.click(confirmButton);

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);

  // Explicit reload fetches the fresh record (disbursed by someone else)…
  mockedApps.fetchApplication.mockResolvedValue(approvedApplication({ stage: "disbursed", version: 6 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/not in the approved stage — disbursement is not/),
  ).toBeInTheDocument();
  // The reload notice reports a post-conflict state: informational
  // styling, NEVER the success variant — and it is announced (W59-4).
  const reloadNotice = screen.getByText(/Record reloaded — re-check the application stage/);
  expect(reloadNotice).toHaveClass("info");
  expect(reloadNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Record reloaded after the conflict — re-check the application stage.",
  );
  // …the action is gone and the stale write was NEVER replayed.
  expect(screen.queryByRole("button", { name: "Disburse…" })).toBeNull();
  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);
});

test("disbursement idempotency keys: stable across retries of an identical intent, rotated when the channel changes", async () => {
  const user = userEvent.setup();
  mocked.disburseApplication
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue({
      loan_id: LOAN_ID,
      txn_id: "eeeeeeee-1111-2222-3333-444444444444",
      txn_ref: "TXN-0001",
      schedule: [SCHEDULE_ROW],
    });
  mountScreen();

  const dialog = await openDisburseDialog(user);
  // Attempt 1 (mpesa) fails with a 5xx…
  await user.click(await confirmDisburse(user, dialog));
  await waitFor(() => expect(mocked.disburseApplication).toHaveBeenCalledTimes(1));

  // …identical retry (same channel) REUSES the key…
  await user.click(await confirmDisburse(user, dialog));
  await waitFor(() => expect(mocked.disburseApplication).toHaveBeenCalledTimes(2));
  const key1 = mocked.disburseApplication.mock.calls[0]?.[2];
  const key2 = mocked.disburseApplication.mock.calls[1]?.[2];
  expect(key1).toBe(key2);

  // …a different intent (bank channel) ROTATES the key.
  await user.click(await confirmDisburse(user, dialog, "bank"));
  await waitFor(() => expect(mocked.disburseApplication).toHaveBeenCalledTimes(3));
  expect(mocked.disburseApplication.mock.calls[2]?.[2]).not.toBe(key1);
});

test("disbursement key freshness (W59-2, the !60 F3 class): after 409 + explicit reload, an identical re-attempt uses a ROTATED key", async () => {
  const user = userEvent.setup();
  mocked.disburseApplication.mockRejectedValue(new ApiError(409, "conflict", "corr-stale"));
  mountScreen();

  const dialog = await openDisburseDialog(user);
  await user.click(await confirmDisburse(user, dialog));
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);
  const key1 = mocked.disburseApplication.mock.calls[0]?.[2];

  // Explicit reload: the record moved underneath (version bumped) but is
  // STILL approved — e.g. a concurrent non-stage edit — so an identical
  // channel re-attempt is now a legitimate NEW intent…
  mockedApps.fetchApplication.mockResolvedValue(approvedApplication({ version: 6 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() => expect(mockedApps.fetchApplication).toHaveBeenCalledTimes(2));

  // …and the reloaded record version in the key material ROTATES the key:
  // the backend idempotency store can never pin the pre-conflict outcome.
  await user.click(await confirmDisburse(user, dialog));
  await waitFor(() => expect(mocked.disburseApplication).toHaveBeenCalledTimes(2));
  expect(mocked.disburseApplication.mock.calls[1]?.[2]).not.toBe(key1);
});

test("repayment: NO write until the byte-identical phrase; double-clicked confirm posts ONCE; the amount travels as the typed STRING; the allocation renders verbatim", async () => {
  const user = userEvent.setup();
  const recorded: RepaymentResult = {
    txn_id: "ffffffff-1111-2222-3333-444444444444",
    txn_ref: "TXN-0007",
    penalties: "1500.10",
    interest: "3499.90",
    principal: "1.10",
    balance_after: "1229566.00",
    penalty_due_after: "0.00",
    status: "active",
  };
  let release: (value: RepaymentResult) => void = () => undefined;
  mocked.recordRepayment.mockImplementation(
    () =>
      new Promise<RepaymentResult>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5001.10");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));

  const confirm = await screen.findByRole("dialog", { name: "Record repayment" });
  const confirmButton = within(confirm).getByRole("button", { name: "Record repayment" });
  // Disabled until the phrase is typed byte-identically — zero writes.
  expect(confirmButton).toBeDisabled();
  expect(mocked.recordRepayment).not.toHaveBeenCalled();

  const phrase = LOAN_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(confirmButton);
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.recordRepayment).toHaveBeenCalledTimes(1);
  // Decimal string end-to-end — never parsed into a float.
  expect(mocked.recordRepayment.mock.calls[0]?.[0]).toBe(LOAN_ID);
  expect(mocked.recordRepayment.mock.calls[0]?.[1]).toEqual({
    amount: "5001.10",
    channel: "mpesa",
  });
  release(recorded);

  // The server's allocation renders verbatim (trailing cents survive).
  expect(await screen.findByText(/Repayment recorded · ref TXN-0007/)).toBeInTheDocument();
  expect(screen.getByText("KES 3,499.90")).toBeInTheDocument();
  expect(screen.getByText("KES 1,229,566.00")).toBeInTheDocument();
});

test("repayment: client Zod verdicts render inline BEFORE any write; server 422 verdicts then WIN per field", async () => {
  const user = userEvent.setup();
  mocked.recordRepayment.mockRejectedValue(
    new ApiError(422, "validation_error", null, [
      { field: "amount", message: "amount exceeds remaining balance policy" },
    ]),
  );
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });

  // Invalid client-side: malformed amount, no channel — zero writes.
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "12.345");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  expect(
    await within(drawer).findByText("Enter a positive amount (up to 2 decimal places)."),
  ).toBeInTheDocument();
  expect(within(drawer).getByText("Select a channel.")).toBeInTheDocument();
  expect(mocked.recordRepayment).not.toHaveBeenCalled();

  // Fix the client issues; the server's field verdict renders inline.
  await user.clear(within(drawer).getByLabelText("Amount (KES)"));
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.00");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "bank");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm = await screen.findByRole("dialog", { name: "Record repayment" });
  const phrase = LOAN_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm).getByRole("button", { name: "Record repayment" }));

  expect(
    await within(drawer).findByText("amount exceeds remaining balance policy"),
  ).toBeInTheDocument();
  expect(mocked.recordRepayment).toHaveBeenCalledTimes(1);
});

test("repayment 409 (loan state moved): explicit reload flow, EXACTLY ONE attempt, reload never replays", async () => {
  const user = userEvent.setup();
  mocked.recordRepayment.mockRejectedValue(new ApiError(409, "conflict", "corr-repay"));
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.00");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm = await screen.findByRole("dialog", { name: "Record repayment" });
  const phrase = LOAN_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm).getByRole("button", { name: "Record repayment" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.recordRepayment).toHaveBeenCalledTimes(1);

  // Explicit reload fetches the fresh record (closed underneath) — the
  // repayment form is withdrawn and nothing is replayed.
  mocked.fetchLoan.mockResolvedValue(baseLoan({ status: "closed", version: 8 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/not active — the contract accepts no further repayments/),
  ).toBeInTheDocument();
  expect(mocked.recordRepayment).toHaveBeenCalledTimes(1);
  // The reload notice reports a post-conflict state: informational
  // styling, NEVER the success variant — and it is announced (W59-4).
  const reloadNotice = screen.getByText(/Record reloaded — re-check the loan/);
  expect(reloadNotice).toHaveClass("info");
  expect(reloadNotice).not.toHaveClass("ok");
  expect(mockedAnnounce).toHaveBeenCalledWith(
    "Record reloaded after the conflict — re-check the loan.",
  );
});

test("repayment key freshness (W59-2, the !60 F3 class): after 409 + explicit reload, an identical re-entry uses a ROTATED key", async () => {
  const user = userEvent.setup();
  mocked.recordRepayment.mockRejectedValue(new ApiError(409, "conflict", "corr-repay"));
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.00");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm = await screen.findByRole("dialog", { name: "Record repayment" });
  const phrase = LOAN_ID.slice(0, 8);
  await user.type(within(confirm).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm).getByRole("button", { name: "Record repayment" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.recordRepayment).toHaveBeenCalledTimes(1);
  const key1 = mocked.recordRepayment.mock.calls[0]?.[2];

  // Explicit reload: the loan moved underneath (version bumped) but is
  // STILL active — an identical re-entry is now a legitimate NEW intent…
  mocked.fetchLoan.mockResolvedValue(baseLoan({ version: 8 }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() => expect(mocked.fetchLoan).toHaveBeenCalledTimes(2));

  // …and the reloaded loan version in the key material ROTATES the key.
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm2 = await screen.findByRole("dialog", { name: "Record repayment" });
  await user.type(within(confirm2).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm2).getByRole("button", { name: "Record repayment" }));

  await waitFor(() => expect(mocked.recordRepayment).toHaveBeenCalledTimes(2));
  expect(mocked.recordRepayment.mock.calls[1]?.[2]).not.toBe(key1);
});

test("per-posting intent counter (W59-3, the !61 T2 class): an IDENTICAL legitimate re-entry after success ROTATES the key — a second wire write is observed, never a silent dedup", async () => {
  const user = userEvent.setup();
  const recorded: RepaymentResult = {
    txn_id: "ffffffff-1111-2222-3333-444444444444",
    txn_ref: "TXN-0007",
    penalties: "0.00",
    interest: "0.00",
    principal: "5000.00",
    balance_after: "1229567.10",
    penalty_due_after: "0.00",
    status: "active",
  };
  mocked.recordRepayment.mockResolvedValue(recorded);
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });
  const phrase = LOAN_ID.slice(0, 8);

  // First posting: 5000.00 via mpesa.
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.00");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm1 = await screen.findByRole("dialog", { name: "Record repayment" });
  await user.type(within(confirm1).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm1).getByRole("button", { name: "Record repayment" }));
  await waitFor(() => expect(mocked.recordRepayment).toHaveBeenCalledTimes(1));
  const key1 = mocked.recordRepayment.mock.calls[0]?.[2];

  // The teller records a SECOND, byte-identical repayment (routine —
  // e.g. two equal instalment payments the same day). The loan version
  // is unchanged in this harness, so ONLY the per-posting intent
  // counter distinguishes the intents.
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.00");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Record repayment…" }));
  const confirm2 = await screen.findByRole("dialog", { name: "Record repayment" });
  await user.type(within(confirm2).getByLabelText(`Type "${phrase}" to confirm`), phrase);
  await user.click(within(confirm2).getByRole("button", { name: "Record repayment" }));

  // A SECOND wire write is observed with a ROTATED key — the server can
  // never silently dedup the legitimate second payment.
  await waitFor(() => expect(mocked.recordRepayment).toHaveBeenCalledTimes(2));
  expect(mocked.recordRepayment.mock.calls[1]?.[1]).toEqual({
    amount: "5000.00",
    channel: "mpesa",
  });
  expect(mocked.recordRepayment.mock.calls[1]?.[2]).not.toBe(key1);
});

test("least-disclosure: a 403 on disbursement renders the sanitized banner only, session intact", async () => {
  const user = userEvent.setup();
  mocked.disburseApplication.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  const dialog = await openDisburseDialog(user);
  await user.click(await confirmDisburse(user, dialog));

  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.disburseApplication).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("query-path 401 tears the session down immediately (dual-cache teardown)", async () => {
  mocked.fetchLoansPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("settlement quote is fetched on demand and rendered verbatim — server figures only", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByText("KES 1,234,567.10"));
  const drawer = await screen.findByRole("dialog", { name: "Loan detail" });
  expect(mocked.fetchSettlementQuote).not.toHaveBeenCalled();

  await user.click(within(drawer).getByRole("button", { name: "Get settlement quote" }));
  expect(await within(drawer).findByText(/Settlement quote · as of 2026-08-03/)).toBeInTheDocument();
  expect(within(drawer).getByText("KES 1,246,067.30")).toBeInTheDocument();
  expect(mocked.fetchSettlementQuote).toHaveBeenCalledTimes(1);
});

test("Zod boundary rejects money as NUMBERS and unknown statuses/classes; the disbursement boundary STRIPS extra keys", () => {
  expect(loanSchema.safeParse(baseLoan()).success).toBe(true);
  expect(loanSchema.safeParse({ ...baseLoan(), balance: 1234567.1 }).success).toBe(false);
  expect(loanSchema.safeParse({ ...baseLoan(), penalty_due: 1500 }).success).toBe(false);
  expect(loanSchema.safeParse({ ...baseLoan(), status: "suspended" }).success).toBe(false);
  expect(loanSchema.safeParse({ ...baseLoan(), classification: "vip" }).success).toBe(false);

  expect(
    portfolioSummarySchema.safeParse({ ...SUMMARY, outstanding_balance: 9876543.1 }).success,
  ).toBe(false);

  // Anything beyond the disbursement contract (internal ledger fields,
  // say) can never reach the DOM: the boundary strips it.
  const parsed = disbursementSchema.parse({
    loan_id: LOAN_ID,
    txn_id: "eeeeeeee-1111-2222-3333-444444444444",
    txn_ref: "TXN-0001",
    schedule: [SCHEDULE_ROW],
    internal_gl_account: "GL-SECRET-01",
  });
  expect("internal_gl_account" in parsed).toBe(false);
});

test("repayment amount rejects leading zeros and zero amounts (W59-5, the !60 F6 class) — pure string shape", () => {
  const parse = (amount: string) =>
    repaymentCreateSchema.safeParse({ amount, channel: "mpesa" });
  // Well-formed decimal strings pass…
  expect(parse("5000").success).toBe(true);
  expect(parse("5000.10").success).toBe(true);
  expect(parse("0.50").success).toBe(true);
  // …leading zeros are rejected (the typed-confirmation banner can
  // never render "KES 007.10")…
  expect(parse("007.10").success).toBe(false);
  expect(parse("050").success).toBe(false);
  expect(parse("00.10").success).toBe(false);
  // …and the existing non-zero refine still holds.
  expect(parse("0").success).toBe(false);
  expect(parse("0.00").success).toBe(false);
});
