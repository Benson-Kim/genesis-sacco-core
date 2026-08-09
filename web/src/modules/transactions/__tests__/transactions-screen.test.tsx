/**
 * Transactions register + detail + teller money writes + interest run
 * adversarial tests THROUGH THE REAL SCREEN CODE (P15 Transactions
 * batch), mirroring the loans/applications reference suites: the
 * feature API layers are stubbed at the module boundary; rendering,
 * keyset paging, permission gating, idempotency-key custody, the fresh
 * member read, 409 handling and the typed confirmations are the real
 * shipped code, mounted under the REAL <Providers>.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { TransactionsScreen } from "../components/TransactionsScreen";
import {
  accountTxnSchema,
  moneyEntrySchema,
  transactionSchema,
  type AccountTxn,
  type InterestRun,
  type Transaction,
} from "../schemas";
import * as txnApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchTransactionsPage: jest.fn(),
    fetchTransactionLegs: jest.fn(),
    postDeposit: jest.fn(),
    postWithdrawal: jest.fn(),
    postShareTopup: jest.fn(),
    postMoneyWrite: jest.fn(),
    runDepositInterest: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
  lookupMemberByNo: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(txnApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const DEBIT_TXN_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const CREDIT_TXN_ID = "bbbbbbbb-1111-2222-3333-444444444444";

// Attacker-influenced free-text fields on this screen — must render as
// inert text (named XSS threat).
const HOSTILE_REF = "<script>window.__pwned=2</script>TXN-REF";
const HOSTILE_NAME = '<img src=x onerror=window.__pwned=1> "quoted" member';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function debitTxn(overrides: Partial<Transaction> = {}): Transaction {
  return {
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
    ...overrides,
  };
}

function creditTxn(overrides: Partial<Transaction> = {}): Transaction {
  return {
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
    ...overrides,
  };
}

const MEMBER = {
  id: MEMBER_ID,
  member_no: "M-0001",
  type: "person" as const,
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active" as const,
  version: 3,
  branch_id: null,
  dividend_payout: null,
};

const ACCOUNT_TXN: AccountTxn = {
  txn_id: "eeeeeeee-1111-2222-3333-444444444444",
  txn_ref: "MP-1099",
  amount: "5000.10",
  balance_after: "173500.10",
};

const INTEREST_RUN: InterestRun = {
  period_start: "2026-04-01",
  period_end: "2026-06-30",
  annual_rate_pct: "8.00",
  scanned: 120,
  posted: 118,
  skipped_existing: 2,
  rate_mismatches: 0,
  total_interest: "45678.10",
  batches: 2,
};

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "transactions", can_view: true, can_create: true, can_edit: true, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** transactions:view ONLY — no create/edit, no members grant. */
const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "transactions", can_view: true, can_create: false, can_edit: false, can_approve: false },
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
      <TransactionsScreen />
    </Providers>,
  );
}

/** Open the post drawer and fill a valid entry (shared by the
 * double-post / 409 / key-custody tests). */
async function fillEntry(
  user: ReturnType<typeof userEvent.setup>,
  {
    amount = "5000.10",
    channel = "mpesa",
    extRef = "SGH3KLM9QT",
  }: { amount?: string; channel?: string; extRef?: string } = {},
) {
  await user.click(await screen.findByRole("button", { name: "+ Post transaction" }));
  const drawer = await screen.findByRole("dialog", { name: "Post transaction" });
  // #35 item 14: the member resolves by UNIQUE number on blur — no
  // full member-list select exists in this drawer any more.
  await user.type(within(drawer).getByLabelText("Member number"), "M-0001");
  await user.tab();
  // The fresh member read must land before the confirmation can arm.
  await within(drawer).findByText(/Member verified:/);
  await user.type(within(drawer).getByLabelText("Amount (KES)"), amount);
  await user.selectOptions(within(drawer).getByLabelText("Channel"), channel);
  // #35 item 6: the external receipt reference is REQUIRED.
  await user.type(within(drawer).getByLabelText("External reference"), extRef);
  return drawer;
}

/** Re-submit the CURRENT drawer entry through the typed confirmation
 * and return the confirm button. */
async function confirmEntry(
  user: ReturnType<typeof userEvent.setup>,
  drawer: HTMLElement,
) {
  await user.click(within(drawer).getByRole("button", { name: "Post to ledger…" }));
  const confirm = await screen.findByRole("dialog", { name: "Post to ledger" });
  await user.type(
    within(confirm).getByLabelText('Type "M-0001" to confirm'),
    "M-0001",
  );
  return within(confirm).getByRole("button", { name: "Post to ledger" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchTransactionsPage.mockResolvedValue(page([debitTxn(), creditTxn()]));
  // Legs drill-down (#31 (g)): a safe default so detail-drawer tests
  // that are not about legs never hit the real network; the legs
  // suite (transaction-legs-screen.test.tsx) owns the assertions.
  mocked.fetchTransactionLegs.mockResolvedValue([]);
  mocked.postMoneyWrite.mockResolvedValue(ACCOUNT_TXN);
  mocked.runDepositInterest.mockResolvedValue(INTEREST_RUN);
  mockedMembers.fetchMembersPage.mockResolvedValue(page([MEMBER]));
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
  mockedMembers.lookupMemberByNo.mockResolvedValue(MEMBER);
});

afterEach(() => {
  clearSession();
});

test("hostile txn ref renders as inert TEXT; DR/CR amounts render VERBATIM in their own columns and are NEVER netted or summed (named XSS threat + blocker (a))", async () => {
  mocked.fetchTransactionsPage.mockResolvedValue(
    page([debitTxn({ txn_ref: HOSTILE_REF }), creditTxn()]),
  );
  const { container } = mountScreen();

  // The payload is visible as literal text…
  expect(await screen.findByText(new RegExp("window.__pwned"))).toBeInTheDocument();
  // …and never materialised as markup.
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();

  // Each amount renders byte-identically in EXACTLY one column —
  // falsifiable: a numeric round-trip drops the trailing ".10"; a
  // client-side reduction would materialise a netted/summed figure.
  expect(screen.getByText("KES 8,000.10")).toBeInTheDocument();
  expect(screen.getByText("KES 5,000.10")).toBeInTheDocument();
  expect(screen.queryByText("KES 3,000.00")).toBeNull(); // no local netting
  expect(screen.queryByText("KES 13,000.20")).toBeNull(); // no local totals row
  expect(screen.queryByText(/Totals/)).toBeNull();
});

test("keyset paging: Load more follows the server cursor VERBATIM — no offset anywhere (gate 1.3)", async () => {
  const user = userEvent.setup();
  mocked.fetchTransactionsPage
    .mockResolvedValueOnce(page([debitTxn()], "opaque-cursor-§1"))
    .mockResolvedValueOnce(page([creditTxn()]));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));

  await waitFor(() => expect(mocked.fetchTransactionsPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchTransactionsPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchTransactionsPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");
});

test("type/channel/direction filters drive the SERVER query — the client never filters locally", async () => {
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 8,000.10");
  await user.selectOptions(screen.getByLabelText("Type"), "withdrawal");
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "withdrawal" }),
      null,
    ),
  );

  await user.selectOptions(screen.getByLabelText("Channel"), "accrual");
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "withdrawal", channel: "accrual" }),
      null,
    ),
  );

  await user.click(screen.getByRole("button", { name: "Debit" }));
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({ direction: "debit" }),
      null,
    ),
  );
});

test("exact-ref + date-range drafts apply on submit as SERVER query params; the member filter drives member_id", async () => {
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 8,000.10");
  await user.type(screen.getByLabelText("Reference (exact)"), "WD-0221");
  await user.type(screen.getByLabelText("From date"), "2026-07-01");
  await user.type(screen.getByLabelText("To date"), "2026-07-31");
  await user.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({
        ref: "WD-0221",
        date_from: "2026-07-01",
        date_to: "2026-07-31",
      }),
      null,
    ),
  );

  await user.selectOptions(screen.getByLabelText("Member"), MEMBER_ID);
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({ member_id: MEMBER_ID }),
      null,
    ),
  );
});

test("UI affordances follow the matrix: a view-only role gets NO posting, NO interest run, NO member filter — and ZERO members fetches", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 8,000.10");
  expect(screen.queryByRole("button", { name: "+ Post transaction" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run deposit interest" })).toBeNull();
  expect(screen.queryByLabelText("Member")).toBeNull();
  // Structural: the stripped role never even FETCHES the members list.
  expect(mockedMembers.fetchMembersPage).not.toHaveBeenCalled();

  // Drill into the detail drawer: without members:view the member stays
  // an opaque id and NO member fetch happens (deny-by-default).
  await user.click(screen.getByText("KES 8,000.10"));
  const drawer = await screen.findByRole("dialog", { name: "Transaction detail" });
  expect(within(drawer).getByText(MEMBER_ID)).toBeInTheDocument();
  expect(mockedMembers.fetchMember).not.toHaveBeenCalled();
});

test("detail drawer resolves the member name (members:view), renders every field VERBATIM and flags reversals", async () => {
  const user = userEvent.setup();
  mockedMembers.fetchMember.mockResolvedValue({ ...MEMBER, name: HOSTILE_NAME });
  mocked.fetchTransactionsPage.mockResolvedValue(
    page([debitTxn({ is_reversal: true, txn_ref: "RV-0001" })]),
  );
  const { container } = mountScreen();

  await user.click(await screen.findByText("KES 8,000.10"));
  const drawer = await screen.findByRole("dialog", { name: "Transaction detail" });

  // The member resolves via GET /members/{id} — hostile name inert.
  expect(await within(drawer).findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
  expect(mockedMembers.fetchMember).toHaveBeenCalledWith(MEMBER_ID);

  expect(within(drawer).getByText("RV-0001")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 8,000.10")).toBeInTheDocument();
  expect(within(drawer).getByText(/REVERSING entry/)).toBeInTheDocument();
  // No running balance is reconstructed client-side.
  expect(within(drawer).getByText(/never reconstructed/)).toBeInTheDocument();
});

test("attribution (issue #30 / !66 follow-up): the detail drawer renders created_by as the SHORT id with the full UUID on title — and NEVER fetches a directory record for it (least disclosure, FM-D)", async () => {
  const TELLER_ID = "66666666-6666-6666-6666-666666666666";
  const user = userEvent.setup();
  mocked.fetchTransactionsPage.mockResolvedValue(
    page([debitTxn({ created_by: TELLER_ID })]),
  );
  mountScreen();

  await user.click(await screen.findByText("KES 8,000.10"));
  const drawer = await screen.findByRole("dialog", { name: "Transaction detail" });
  // Short-id convention: the 8-char prefix renders; the full UUID rides
  // the title attribute (same treatment as every other opaque id).
  const cell = within(drawer).getByTitle(TELLER_ID);
  expect(cell).toHaveTextContent(TELLER_ID.slice(0, 8));
  // Least disclosure: the staff UUID is NEVER resolved to a person —
  // the only member fetch is the ledger row's member, never the teller.
  for (const call of mockedMembers.fetchMember.mock.calls) {
    expect(call[0]).not.toBe(TELLER_ID);
  }
});

test("attribution NULL leg (FM-B): a system/unattributed posting renders the honest affordance — never an invented actor", async () => {
  const user = userEvent.setup();
  mountScreen();

  // Base fixtures carry created_by: null (system/job postings and
  // pre-0036 rows are the documented NULL branch).
  await user.click(await screen.findByText("KES 8,000.10"));
  const drawer = await screen.findByRole("dialog", { name: "Transaction detail" });
  expect(
    within(drawer).getByText("— (system posting or unattributed)"),
  ).toBeInTheDocument();
});

test("attribution XSS inertness: a hostile created_by string renders as inert TEXT in the UUID cell — no markup materialises", async () => {
  const HOSTILE_CREATOR = "<img src=x onerror=window.__pwned=4>";
  const user = userEvent.setup();
  mocked.fetchTransactionsPage.mockResolvedValue(
    page([debitTxn({ created_by: HOSTILE_CREATOR })]),
  );
  const { container } = mountScreen();

  await user.click(await screen.findByText("KES 8,000.10"));
  const drawer = await screen.findByRole("dialog", { name: "Transaction detail" });
  // The sliced 8-char prefix ("<img src") renders as literal text…
  expect(within(drawer).getByTitle(HOSTILE_CREATOR)).toHaveTextContent("<img src");
  // …and never as an element.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("DOUBLE-POST impossible from this client: triple-clicked confirmation produces exactly ONE write; the affordance is SPENT after success", async () => {
  const user = userEvent.setup();
  let release: (value: AccountTxn) => void = () => undefined;
  mocked.postMoneyWrite.mockImplementation(
    () =>
      new Promise<AccountTxn>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  const drawer = await fillEntry(user);
  const confirmButton = await confirmEntry(user, drawer);
  await user.click(confirmButton);
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
  expect(mocked.postMoneyWrite.mock.calls[0]?.[0]).toBe("deposit");
  expect(mocked.postMoneyWrite.mock.calls[0]?.[1]).toBe(MEMBER_ID);
  // Decimal string end-to-end — never parsed into a float.
  expect(mocked.postMoneyWrite.mock.calls[0]?.[2]).toEqual({
    amount: "5000.10",
    channel: "mpesa",
    external_ref: "SGH3KLM9QT",
  });

  release(ACCOUNT_TXN);

  // The result panel replaces the form — the affordance is spent…
  expect(await screen.findByText(/Deposit posted · ref MP-1099/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Post to ledger…" })).toBeNull();
  // …the SERVER's balance renders verbatim (trailing cents survive)…
  expect(screen.getByText("KES 173,500.10")).toBeInTheDocument();
  // …still exactly one write.
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
});

test("STALE-READ DOUBLE-POST PREVENTION: the drawer FRESH-READS the member (staleTime 0) before arming; reopening re-fetches instead of serving cache", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await fillEntry(user);
  expect(mockedMembers.fetchMember).toHaveBeenCalledTimes(1);

  // NO write can be confirmed before the fresh read has landed: the
  // confirmation dialog only mounts with the fresh member record.
  await user.click(within(drawer).getByRole("button", { name: "Post to ledger…" }));
  await screen.findByRole("dialog", { name: "Post to ledger" });
  expect(mocked.postMoneyWrite).not.toHaveBeenCalled();

  // Close everything and reopen: record-class staleTime 0 re-fetches —
  // a cached member record here would feed a STALE read into a money
  // write (THE failure mode of this module).
  await user.click(
    within(screen.getByRole("dialog", { name: "Post to ledger" })).getByRole("button", {
      name: "Cancel",
    }),
  );
  await user.click(within(drawer).getByRole("button", { name: "Cancel" }));
  await user.click(await screen.findByRole("button", { name: "+ Post transaction" }));
  const reopened = await screen.findByRole("dialog", { name: "Post transaction" });
  await user.type(within(reopened).getByLabelText("Member number"), "M-0001");
  await user.tab();
  await waitFor(() => expect(mockedMembers.fetchMember).toHaveBeenCalledTimes(2));
});

test("idempotency keys: STABLE across retries of an identical intent, ROTATED when the amount changes", async () => {
  const user = userEvent.setup();
  mocked.postMoneyWrite
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(ACCOUNT_TXN);
  mountScreen();

  const drawer = await fillEntry(user);
  // Attempt 1 fails with a 5xx…
  await user.click(await confirmEntry(user, drawer));
  await waitFor(() => expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1));

  // …identical retry (same entry) REUSES the key…
  await user.click(await confirmEntry(user, drawer));
  await waitFor(() => expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(2));
  const key1 = mocked.postMoneyWrite.mock.calls[0]?.[3];
  const key2 = mocked.postMoneyWrite.mock.calls[1]?.[3];
  expect(key1).toBe(key2);

  // …a different intent (changed amount) ROTATES the key.
  const amountField = within(drawer).getByLabelText("Amount (KES)");
  await user.clear(amountField);
  await user.type(amountField, "7000.10");
  await user.click(await confirmEntry(user, drawer));
  await waitFor(() => expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(3));
  expect(mocked.postMoneyWrite.mock.calls[2]?.[3]).not.toBe(key1);
});

test("withdrawal 409 (balance moved under the row lock): ConflictBanner with EXACTLY ONE attempt; reload re-fetches the member, announces the withdrawal and NEVER replays; the re-entered intent gets a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.postMoneyWrite
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-txn"))
    .mockResolvedValue(ACCOUNT_TXN);
  mountScreen();

  const drawer = await fillEntry(user, { channel: "bank" });
  await user.click(await confirmEntry(user, drawer));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
  const staleKey = mocked.postMoneyWrite.mock.calls[0]?.[3];

  // Explicit reload: the member re-fetches; the failed posting is
  // withdrawn with an informational (NOT success-styled) notice (!60 F5).
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() => expect(mockedMembers.fetchMember).toHaveBeenCalledTimes(2));
  expect(
    await screen.findByText(/conflicted posting was withdrawn, nothing was replayed/),
  ).toBeInTheDocument();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);

  // Re-entering the IDENTICAL entry is a NEW intent: the reload epoch
  // rotates the key, so a backend idempotency store pinned to the
  // conflicted first attempt can never serve the stale outcome.
  await user.click(await confirmEntry(user, drawer));
  await waitFor(() => expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(2));
  expect(mocked.postMoneyWrite.mock.calls[1]?.[3]).not.toBe(staleKey);
});

test("an EXITED member structurally withdraws the posting affordance — zero writes (the server enforces regardless)", async () => {
  const user = userEvent.setup();
  mockedMembers.fetchMember.mockResolvedValue({ ...MEMBER, status: "exited" });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Post transaction" }));
  const drawer = await screen.findByRole("dialog", { name: "Post transaction" });
  await user.type(within(drawer).getByLabelText("Member number"), "M-0001");
  await user.tab();

  expect(await within(drawer).findByText(/has EXITED/)).toBeInTheDocument();
  const submit = within(drawer).getByRole("button", { name: "Post to ledger…" });
  expect(submit).toBeDisabled();
  expect(mocked.postMoneyWrite).not.toHaveBeenCalled();
});

test("client Zod verdicts render inline BEFORE any write (leading zeros rejected — !60 F6); server 422 verdicts then WIN per field", async () => {
  const user = userEvent.setup();
  mocked.postMoneyWrite.mockRejectedValue(
    new ApiError(422, "validation_error", null, [
      { field: "amount", message: "amount exceeds the teller posting policy" },
    ]),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Post transaction" }));
  const drawer = await screen.findByRole("dialog", { name: "Post transaction" });
  await user.type(within(drawer).getByLabelText("Member number"), "M-0001");
  await user.tab();

  // Leading-zero amount, no channel — zero writes.
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "007.10");
  await user.click(within(drawer).getByRole("button", { name: "Post to ledger…" }));
  expect(
    await within(drawer).findByText(
      "Enter a positive amount (up to 2 decimal places, no leading zeros).",
    ),
  ).toBeInTheDocument();
  expect(within(drawer).getByText("Select a channel.")).toBeInTheDocument();
  expect(mocked.postMoneyWrite).not.toHaveBeenCalled();

  // Fix the client issues; the server's field verdict renders inline.
  const amountField = within(drawer).getByLabelText("Amount (KES)");
  await user.clear(amountField);
  await user.type(amountField, "7.10");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "bank");
  await user.type(within(drawer).getByLabelText("External reference"), "SLIP-88");
  await user.click(await confirmEntry(user, drawer));

  expect(
    await within(drawer).findByText("amount exceeds the teller posting policy"),
  ).toBeInTheDocument();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
});

test("least-disclosure: a 403 on posting renders the sanitized banner only, session intact", async () => {
  const user = userEvent.setup();
  mocked.postMoneyWrite.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  const drawer = await fillEntry(user);
  await user.click(await confirmEntry(user, drawer));

  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("query-path 401 tears the session down immediately (dual-cache teardown; this module holds NO per-tab registry to clear)", async () => {
  mocked.fetchTransactionsPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("interest run: typed confirmation, double-clicked confirm runs ONCE, the SERVER's run figures render verbatim, the affordance is SPENT", async () => {
  const user = userEvent.setup();
  let release: (value: InterestRun) => void = () => undefined;
  mocked.runDepositInterest.mockImplementation(
    () =>
      new Promise<InterestRun>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Run deposit interest" }));
  const dialog = await screen.findByRole("dialog", { name: "Run deposit interest" });
  await user.click(within(dialog).getByRole("button", { name: "Run accrual…" }));

  const confirm = await screen.findByRole("dialog", { name: "Accrue deposit interest" });
  const confirmButton = within(confirm).getByRole("button", { name: "Accrue deposit interest" });
  // Disabled until the phrase is typed byte-identically — zero writes.
  expect(confirmButton).toBeDisabled();
  expect(mocked.runDepositInterest).not.toHaveBeenCalled();
  await user.type(
    within(confirm).getByLabelText('Type "accrue interest" to confirm'),
    "accrue interest",
  );
  await user.click(confirmButton);
  await user.click(confirmButton);

  expect(mocked.runDepositInterest).toHaveBeenCalledTimes(1);
  release(INTEREST_RUN);

  expect(await screen.findByText(/Interest accrued · 2026-04-01 → 2026-06-30/)).toBeInTheDocument();
  expect(screen.getByText("KES 45,678.10")).toBeInTheDocument();
  expect(screen.getByText("8.00% (tenant-configured)")).toBeInTheDocument();
  // Spent: the action is replaced by the result panel.
  expect(screen.queryByRole("button", { name: "Run accrual…" })).toBeNull();
  expect(mocked.runDepositInterest).toHaveBeenCalledTimes(1);
});

test("interest run 409 (unconfigured rate / no elapsed quarter): sanitized banner + operator note — a CREATE-style conflict with nothing to reload, nothing retried", async () => {
  const user = userEvent.setup();
  mocked.runDepositInterest.mockRejectedValue(new ApiError(409, "conflict", "corr-int"));
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Run deposit interest" }));
  const dialog = await screen.findByRole("dialog", { name: "Run deposit interest" });
  await user.click(within(dialog).getByRole("button", { name: "Run accrual…" }));
  const confirm = await screen.findByRole("dialog", { name: "Accrue deposit interest" });
  await user.type(
    within(confirm).getByLabelText('Type "accrue interest" to confirm'),
    "accrue interest",
  );
  await user.click(within(confirm).getByRole("button", { name: "Accrue deposit interest" }));

  expect(
    await within(dialog).findByText(/The record changed or conflicts with existing data/),
  ).toBeInTheDocument();
  expect(within(dialog).getByText(/rate is not configured/)).toBeInTheDocument();
  // No record-reload affordance exists for this unversioned run.
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.runDepositInterest).toHaveBeenCalledTimes(1);
});

test("IDENTICAL re-entry after 'Post another' is a NEW intent: the key ROTATES and a SECOND wire write happens — no silent ledger omission (review T2)", async () => {
  const user = userEvent.setup();
  mountScreen();

  // First posting succeeds.
  const drawer = await fillEntry(user);
  await user.click(await confirmEntry(user, drawer));
  expect(await screen.findByText(/Deposit posted · ref MP-1099/)).toBeInTheDocument();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
  const firstKey = mocked.postMoneyWrite.mock.calls[0]?.[3];

  // "Post another" + the byte-identical entry (two equal cash deposits
  // the same day — a routine SACCO flow): the intent counter rotates
  // the key, so the server can NEVER dedup the second posting into the
  // first response while the operator sees success.
  await user.click(within(drawer).getByRole("button", { name: "Post another" }));
  await user.type(within(drawer).getByLabelText("Amount (KES)"), "5000.10");
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  // The ref field CLEARED with the spent intent (#35 item 6): the same
  // physical M-Pesa code must never ride a second posting by default —
  // the teller types the second receipt's code.
  await user.type(within(drawer).getByLabelText("External reference"), "SGH3KLM9QU");
  await user.click(await confirmEntry(user, drawer));

  expect(await screen.findByText(/Deposit posted · ref MP-1099/)).toBeInTheDocument();
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(2);
  expect(mocked.postMoneyWrite.mock.calls[1]?.[2]).toEqual({
    amount: "5000.10",
    channel: "mpesa",
    external_ref: "SGH3KLM9QU",
  });
  expect(mocked.postMoneyWrite.mock.calls[1]?.[3]).not.toBe(firstKey);
});

test("interest-run key custody: STABLE across pure retries (5xx then 409 reuse one key), ROTATED after the acknowledged conflict (review T3)", async () => {
  const user = userEvent.setup();
  mocked.runDepositInterest
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-2"))
    .mockResolvedValue(INTEREST_RUN);
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Run deposit interest" }));
  const dialog = await screen.findByRole("dialog", { name: "Run deposit interest" });

  async function attempt() {
    await user.click(within(dialog).getByRole("button", { name: "Run accrual…" }));
    const confirm = await screen.findByRole("dialog", { name: "Accrue deposit interest" });
    await user.type(
      within(confirm).getByLabelText('Type "accrue interest" to confirm'),
      "accrue interest",
    );
    await user.click(within(confirm).getByRole("button", { name: "Accrue deposit interest" }));
  }

  // 5xx, then the conflict attempt: both are retries of ONE intent —
  // the key stays STABLE.
  await attempt();
  await waitFor(() => expect(mocked.runDepositInterest).toHaveBeenCalledTimes(1));
  await attempt();
  await waitFor(() => expect(mocked.runDepositInterest).toHaveBeenCalledTimes(2));
  const key1 = mocked.runDepositInterest.mock.calls[0]?.[0];
  expect(mocked.runDepositInterest.mock.calls[1]?.[0]).toBe(key1);

  // The operator fixes the cause (e.g. configures the rate) and retries
  // in the SAME dialog: a NEW intent — the key ROTATES, so a backend
  // idempotency store pinned to the 409 can never replay it.
  await attempt();
  await waitFor(() => expect(mocked.runDepositInterest).toHaveBeenCalledTimes(3));
  expect(mocked.runDepositInterest.mock.calls[2]?.[0]).not.toBe(key1);
  expect(await screen.findByText(/Interest accrued · 2026-04-01 → 2026-06-30/)).toBeInTheDocument();
});

test("malformed manual date drafts never become server query params (review T4)", async () => {
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 8,000.10");
  const callsBefore = mocked.fetchTransactionsPage.mock.calls.length;
  // type="date" can be bypassed by manual entry in some browsers — the
  // ISO shape is validated before it reaches the query.
  const fromField = screen.getByLabelText("From date");
  fromField.removeAttribute("type"); // simulate a browser without date support
  await user.type(fromField, "18/07/2026");
  await user.click(screen.getByRole("button", { name: "Apply" }));

  expect(await screen.findByText("Enter dates as YYYY-MM-DD.")).toBeInTheDocument();
  // No new list fetch with the malformed value.
  expect(mocked.fetchTransactionsPage.mock.calls.length).toBe(callsBefore);
  for (const call of mocked.fetchTransactionsPage.mock.calls) {
    expect((call[0] as { date_from: string }).date_from).toBe("");
  }
});

test("Zod boundary rejects money as NUMBERS, unknown enums and leading-zero inputs; the account-txn boundary STRIPS extra keys", () => {
  expect(transactionSchema.safeParse(debitTxn()).success).toBe(true);
  expect(transactionSchema.safeParse({ ...debitTxn(), amount: 8000.1 }).success).toBe(false);
  expect(transactionSchema.safeParse({ ...debitTxn(), type: "gl_sweep" }).success).toBe(false);
  expect(transactionSchema.safeParse({ ...debitTxn(), channel: "cash" }).success).toBe(false);
  expect(transactionSchema.safeParse({ ...debitTxn(), direction: "net" }).success).toBe(false);

  // Anything beyond the AccountTxnOut contract can never reach the DOM.
  const parsed = accountTxnSchema.parse({ ...ACCOUNT_TXN, internal_gl_note: "GL-SECRET-01" });
  expect("internal_gl_note" in parsed).toBe(false);
  expect(accountTxnSchema.safeParse({ ...ACCOUNT_TXN, balance_after: 173500.1 }).success).toBe(
    false,
  );

  // Money INPUT hygiene (!60 F6): pure string shape — leading zeros and
  // zero amounts rejected, honest cents accepted.
  // external_ref is REQUIRED since #35 item 6 — a valid M-Pesa code
  // keeps these legs probing ONLY the amount shape.
  const entry = {
    kind: "deposit",
    member_id: MEMBER_ID,
    channel: "mpesa",
    external_ref: "SGH3KLM9QT",
  };
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "007.10" }).success).toBe(false);
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "0" }).success).toBe(false);
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "0.00" }).success).toBe(false);
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "12.345" }).success).toBe(false);
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "0.50" }).success).toBe(true);
  expect(moneyEntrySchema.safeParse({ ...entry, amount: "5000.10" }).success).toBe(true);
});

test("W56-3 inheritance (post-!62 merge): a stray overlay click never discards a dirty teller posting or the armed interest-run dialog; the read-only detail drawer keeps the light dismissal", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  // Money-form drawer: the half-completed entry SURVIVES the stray click…
  const drawer = await fillEntry(user);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Post transaction" })).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Amount (KES)")).toHaveValue("5000.10");
  // …and the explicit ✕ still closes.
  await user.click(within(drawer).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Post transaction" })).toBeNull();

  // Money-write dialog: the interest-run flow is NOT discarded by a
  // stray click outside the dialog.
  await user.click(screen.getByRole("button", { name: "Run deposit interest" }));
  const dialog = await screen.findByRole("dialog", { name: "Run deposit interest" });
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Run deposit interest" })).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Run deposit interest" })).toBeNull();

  // The read-only detail drawer is NOT form-bearing — the default light
  // overlay dismissal is the convention there (falsifiable: a blanket
  // opt-out would fail this branch).
  await user.click(screen.getByText("KES 8,000.10"));
  await screen.findByRole("dialog", { name: "Transaction detail" });
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.queryByRole("dialog", { name: "Transaction detail" })).toBeNull();
});

test("amount PASTE/IME hygiene (#31 ledger (h5)): a clipboard-grouped figure and IME-composed full-width digits are REJECTED inline as decimal-string violations — ZERO wire writes", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Post transaction" }));
  const drawer = await screen.findByRole("dialog", { name: "Post transaction" });
  await user.type(within(drawer).getByLabelText("Member number"), "M-0001");
  await user.tab();
  await within(drawer).findByText(/Member verified:/);
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.type(within(drawer).getByLabelText("External reference"), "SGH3KLM9QT");
  const amount = within(drawer).getByLabelText("Amount (KES)");

  // PASTE leg: "5,000.00" — the spreadsheet-grouped clipboard shape.
  // The value lands VERBATIM in the input (no silent normalisation —
  // stripping the comma would guess at the operator's intent) and the
  // decimal-string schema refuses it before anything reaches the wire.
  await user.click(amount);
  await user.paste("5,000.00");
  expect(amount).toHaveValue("5,000.00");
  await user.click(within(drawer).getByRole("button", { name: "Post to ledger…" }));
  expect(
    await within(drawer).findByText(
      "Enter a positive amount (up to 2 decimal places, no leading zeros).",
    ),
  ).toBeInTheDocument();
  // No typed-confirmation dialog armed, nothing posted.
  expect(screen.queryByRole("dialog", { name: "Post to ledger" })).toBeNull();
  expect(mocked.postMoneyWrite).not.toHaveBeenCalled();

  // IME leg: full-width composed digits "５０００" (an East-Asian IME
  // committing FULLWIDTH DIGIT code points, U+FF15 etc.) are NOT the
  // ASCII decimal grammar — refused the same way, zero writes.
  await user.clear(amount);
  await user.click(amount);
  await user.paste("\uFF15\uFF10\uFF10\uFF10");
  expect(amount).toHaveValue("５０００");
  await user.click(within(drawer).getByRole("button", { name: "Post to ledger…" }));
  expect(
    await within(drawer).findByText(
      "Enter a positive amount (up to 2 decimal places, no leading zeros).",
    ),
  ).toBeInTheDocument();
  expect(mocked.postMoneyWrite).not.toHaveBeenCalled();

  // The honest ASCII entry still posts — hygiene is exactness, not
  // lockout (falsifiable: loosen DECIMAL_RE to accept the comma or
  // full-width digits and the two rejection legs above fail).
  await user.clear(amount);
  await user.type(amount, "5000.10");
  await user.click(await confirmEntry(user, drawer));
  await within(drawer).findByText(/posted · ref/);
  expect(mocked.postMoneyWrite).toHaveBeenCalledTimes(1);
});


test("#35 item 13 date presets compute EXPLICIT from/to params client-side — the server never sees a preset token; All clears the keys", async () => {
  // Pin ONLY the clock (Date), keep real timers for react-query.
  jest.useFakeTimers({
    now: new Date(2026, 7, 8, 12, 0, 0), // local 2026-08-08
    doNotFake: [
      "setTimeout",
      "setInterval",
      "clearTimeout",
      "clearInterval",
      "queueMicrotask",
      "setImmediate",
      "clearImmediate",
      "performance",
      "hrtime",
      "nextTick",
    ],
  });
  try {
    const user = userEvent.setup();
    mountScreen();
    await screen.findByText("KES 8,000.10");

    function lastFilters(): { date_from: string; date_to: string } {
      const call = mocked.fetchTransactionsPage.mock.calls.at(-1);
      return call?.[0] as { date_from: string; date_to: string };
    }

    // Hand-computed oracles for a 2026-08-08 clock:
    //   Today        => [2026-08-08, 2026-08-08]
    //   Last 7 days  => [2026-08-02, 2026-08-08]  (8 - 6 = 2)
    //   Last 30 days => [2026-07-10, 2026-08-08]  (Aug 8 - 29d = Jul 10)
    await user.click(screen.getByRole("button", { name: "Today" }));
    await waitFor(() =>
      expect(lastFilters()).toMatchObject({ date_from: "2026-08-08", date_to: "2026-08-08" }),
    );

    await user.click(screen.getByRole("button", { name: "Last 7 days" }));
    await waitFor(() =>
      expect(lastFilters()).toMatchObject({ date_from: "2026-08-02", date_to: "2026-08-08" }),
    );

    await user.click(screen.getByRole("button", { name: "Last 30 days" }));
    await waitFor(() =>
      expect(lastFilters()).toMatchObject({ date_from: "2026-07-10", date_to: "2026-08-08" }),
    );

    // The computed window is VISIBLE in the manual inputs (the operator
    // can read exactly what was sent — no hidden state).
    expect(screen.getByLabelText("From date")).toHaveValue("2026-07-10");
    expect(screen.getByLabelText("To date")).toHaveValue("2026-08-08");

    // All clears the date keys — the default view sends no date filter.
    // (Switching back to the default re-uses the CACHED mount query, so
    // the wire contract is pinned by the mount call below, and the
    // cleared state by the visible inputs + pressed state.)
    await user.click(screen.getByRole("button", { name: "All", pressed: false }));
    await waitFor(() => expect(screen.getByLabelText("From date")).toHaveValue(""));
    expect(screen.getByLabelText("To date")).toHaveValue("");
    const mountFilters = mocked.fetchTransactionsPage.mock.calls[0]?.[0] as {
      date_from: string;
      date_to: string;
    };
    expect(mountFilters).toMatchObject({ date_from: "", date_to: "" });

    // NOTHING preset-shaped ever reached the wire: every call's date
    // params are either empty or ISO dates (falsifiable: send the
    // token and this fails).
    for (const call of mocked.fetchTransactionsPage.mock.calls) {
      const filters = call[0] as { date_from: string; date_to: string };
      for (const value of [filters.date_from, filters.date_to]) {
        expect(value === "" || /^\d{4}-\d{2}-\d{2}$/.test(value)).toBe(true);
      }
    }

    // Custom range keeps the manual flow: editing + Apply moves the
    // pressed state to Custom and applies the typed dates verbatim.
    await user.click(screen.getByRole("button", { name: "Custom range" }));
    const fromField = screen.getByLabelText("From date");
    const toField = screen.getByLabelText("To date");
    await user.clear(fromField);
    await user.type(fromField, "2026-06-01");
    await user.clear(toField);
    await user.type(toField, "2026-06-30");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastFilters()).toMatchObject({ date_from: "2026-06-01", date_to: "2026-06-30" }),
    );
    expect(screen.getByRole("button", { name: "Custom range" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  } finally {
    jest.useRealTimers();
  }
});

test("#35 W1: stale Custom dates never leak into a LATER real fetch — after All, the next pagination fetch carries EMPTY date params", async () => {
  const user = userEvent.setup();
  // Every FIRST page serves a cursor so "Load more" can force a REAL
  // wire fetch after the cached default page is re-selected; the
  // follow-on page carries a distinct row (no duplicate row keys).
  mocked.fetchTransactionsPage.mockImplementation((_filters, cursor) =>
    Promise.resolve(cursor === null ? page([debitTxn()], "w1-cursor-p2") : page([creditTxn()])),
  );
  mountScreen();
  await screen.findByText("KES 8,000.10");

  // Apply a Custom range through the manual drafts (the W1 scenario).
  await user.type(screen.getByLabelText("From date"), "2026-06-01");
  await user.type(screen.getByLabelText("To date"), "2026-06-30");
  await user.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage).toHaveBeenCalledWith(
      expect.objectContaining({ date_from: "2026-06-01", date_to: "2026-06-30" }),
      null,
    ),
  );
  expect(screen.getByRole("button", { name: "Custom range" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  // Back to All: react-query re-serves the CACHED default page — the
  // cleared state is visible but no new wire fetch fires yet.
  await user.click(screen.getByRole("button", { name: "All", pressed: false }));
  await waitFor(() => expect(screen.getByLabelText("From date")).toHaveValue(""));
  const callsBeforeRefetch = mocked.fetchTransactionsPage.mock.calls.length;

  // Force a REAL wire fetch on the restored default key: paginate.
  await user.click(screen.getByRole("button", { name: "Load more" }));
  await waitFor(() =>
    expect(mocked.fetchTransactionsPage.mock.calls.length).toBe(callsBeforeRefetch + 1),
  );

  // THE W1 ASSERTION: the later real fetch carries EMPTY date params —
  // the stale Custom window survived nowhere in the query state
  // (falsifiable: skip the date_from/date_to reset in the All branch
  // of applyDatePreset and this fetch carries 2026-06-01/2026-06-30).
  const laterCall = mocked.fetchTransactionsPage.mock.calls.at(-1);
  expect(laterCall?.[0]).toMatchObject({ date_from: "", date_to: "" });
  expect(laterCall?.[1]).toBe("w1-cursor-p2");
});
