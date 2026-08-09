/**
 * Corrections console adversarial tests THROUGH THE REAL SCREEN CODE
 * (P15 follow-on batch 1, issue #31 — audit #30 finding R1: the
 * P13.15 fraud channel). The feature API layer is stubbed at the
 * module boundary; rendering, mutation orchestration, 409 handling,
 * idempotency-key custody, permission gating, the SoD structural
 * withholding and the typed confirmations are the real shipped code,
 * mounted under the REAL <Providers> (mutations retry:0, the 401
 * teardown — production configuration; falsifiable by flipping either).
 *
 * Centrepieces (the users/exits reference-harness pyramid):
 * - MONEY (blocker (a)): every figure renders VERBATIM; the fixtures
 *   deliberately break the component-sum identities (the adjustment
 *   allocation and the write-off snapshot) so any client-side
 *   re-derivation renders a figure these tests refuse to find.
 * - SoD: adjustments consume the CONTRACT's maker_id (server truth —
 *   the !70 pattern); write-offs fall back to the per-tab registry
 *   witness (WriteOffOut does not serialise requested_by — contract
 *   follow-up on issue #31) with the honest UNKNOWN leg.
 * - Votes are SPENT across remounts (votedRegistry, W58-6); BOTH
 *   registries die on the query-path 401 (W58-2).
 * - EXACTLY ONE write per intent everywhere; 409s get the explicit
 *   reload flow and a ROTATED key on re-entry (!60 F3/F5).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { CorrectionsScreen } from "../components/CorrectionsScreen";
import {
  clearWriteOffMakers,
  recordWriteOffMaker,
  WRITE_OFF_MAKER_UNKNOWN,
  writeOffMakerOf,
} from "../makerRegistry";
import { clearVotedWriteOffs, hasVotedOnWriteOff, recordVotedWriteOff } from "../votedRegistry";
import { adjustmentSchema, writeOffSchema, type AdjustmentRecord, type WriteOffRecord } from "../schemas";
import * as correctionsApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    requestAdjustment: jest.fn(),
    fetchAdjustment: jest.fn(),
    fetchAdjustmentsPage: jest.fn(),
    approveAdjustment: jest.fn(),
    rejectAdjustment: jest.fn(),
    postFee: jest.fn(),
    requestWriteOff: jest.fn(),
    fetchWriteOff: jest.fn(),
    fetchWriteOffsPage: jest.fn(),
    voteOnWriteOff: jest.fn(),
    voidWriteOff: jest.fn(),
    postWriteOff: jest.fn(),
    recordRecoveryReceipt: jest.fn(),
    fetchRecoveryReceiptsPage: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(correctionsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const OTHER_OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const REPAYMENT_ID = "33333333-4444-5555-6666-777777777777";
const ADJ_ID = "aaaaaaaa-1111-2222-3333-444444444444";
const WO_ID = "bbbbbbbb-1111-2222-3333-444444444444";

const ADJ_PHRASE = ADJ_ID.slice(0, 8);
const WO_PHRASE = WO_ID.slice(0, 8);

// Attacker-influenced free text on this module (the reason fields) —
// must render as inert text (named XSS threat). Built without the
// literal cookie-sink token (client-hygiene grep gate).
const HOSTILE_REASON = '<img src=x onerror=window.__pwned=1> "quoted" reason';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

/** amount is DELIBERATELY not the sum of the three component legs
 * (450.10): the identity is the DB CHECK's — a client-side
 * re-derivation would render KES 450.10, which these tests refuse to
 * find. */
function pendingAdjustment(overrides: Partial<AdjustmentRecord> = {}): AdjustmentRecord {
  return {
    id: ADJ_ID,
    repayment_id: REPAYMENT_ID,
    loan_id: LOAN_ID,
    original_transaction_id: "44444444-5555-6666-7777-888888888888",
    reversal_transaction_id: null,
    maker_id: OTHER_OFFICER_ID,
    checker_id: null,
    reason: "Duplicate repayment posting",
    amount: "500.00",
    penalties: "100.10",
    interest: "150.00",
    principal: "200.00",
    would_reopen_loan: false,
    status: "pending_approval",
    loan_balance_at_request: "40000.00",
    loan_penalty_due_at_request: "0.00",
    loan_status_at_request: "active",
    decided_at: null,
    version: 3,
    created_at: "2026-08-01T09:00:00+00:00",
    ...overrides,
  };
}

const ADJUSTMENT_RESULT = {
  adjustment_id: ADJ_ID,
  reversal_txn_id: "55555555-6666-7777-8888-999999999999",
  reversal_txn_ref: "ADJ-0001",
  amount: "500.00",
  penalties: "100.10",
  interest: "150.00",
  principal: "200.00",
  balance_after: "40500.00",
  penalty_due_after: "100.10",
  loan_status: "active",
  reopened: false,
};

/** total_written_off is DELIBERATELY not the component sum
 * (41500.10) — same falsifiable no-client-math posture. */
function requestedWriteOff(overrides: Partial<WriteOffRecord> = {}): WriteOffRecord {
  return {
    id: WO_ID,
    loan_id: LOAN_ID,
    member_id: MEMBER_ID,
    balance: "40000.00",
    penalty_due: "1500.10",
    total_written_off: "77777.77",
    classification: "loss",
    provision_pct: "100.00",
    reason: "Member absconded",
    status: "requested",
    decided_at: null,
    posted_at: null,
    transaction_id: null,
    version: 2,
    created_at: "2026-08-01T09:00:00+00:00",
    ...overrides,
  };
}

function postedWriteOff(overrides: Partial<WriteOffRecord> = {}): WriteOffRecord {
  return requestedWriteOff({
    status: "posted",
    decided_at: "2026-08-02T10:00:00+00:00",
    posted_at: "2026-08-03T10:00:00+00:00",
    transaction_id: "66666666-7777-8888-9999-aaaaaaaaaaaa",
    version: 4,
    ...overrides,
  });
}

const VOTE_TALLY_OPEN = {
  approvals: 1,
  rejections: 0,
  decision: null,
  status: "requested" as const,
};

const WRITE_OFF_POST_RESULT = {
  write_off_id: WO_ID,
  loan_id: LOAN_ID,
  txn_id: "66666666-7777-8888-9999-aaaaaaaaaaaa",
  txn_ref: "WO-0001",
  total_written_off: "77777.77",
  status: "posted" as const,
};

const RECEIPT_RESULT = {
  recovery_id: "77777777-8888-9999-aaaa-bbbbbbbbbbbb",
  write_off_id: WO_ID,
  loan_id: LOAN_ID,
  member_id: MEMBER_ID,
  txn_id: "88888888-9999-aaaa-bbbb-cccccccccccc",
  txn_ref: "RC-0001",
  amount: "5000.50",
  recovered_total: "5000.50",
  outstanding_claim: "72777.27",
  claim_fully_recovered: false,
  guarantees_released: 0,
  recovery_case_id: null,
};

const EMPTY_RECEIPTS_PAGE = {
  items: [],
  recovered_total: "0.00",
  outstanding_claim: "77777.77",
  next_cursor: null,
};

const FEE_RESULT = {
  txn_id: "99999999-aaaa-bbbb-cccc-dddddddddddd",
  txn_ref: "FE-0001",
  fee_type: "registration" as const,
  amount: "1000.00",
};

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

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "corrections", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

/** corrections:view ONLY — no create (maker) and no approve (checker). */
const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "corrections",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS | typeof VIEW_ONLY_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountScreen() {
  return render(
    <Providers>
      <CorrectionsScreen />
    </Providers>,
  );
}

/** Open the adjustment review drawer through the by-id lookup (cleared
 * first — the field keeps its value across drawer opens). */
async function openAdjustmentDetail(user: ReturnType<typeof userEvent.setup>) {
  const lookup = await screen.findByLabelText("Review adjustment by id");
  await user.clear(lookup);
  await user.type(lookup, ADJ_ID);
  await user.click(screen.getByRole("button", { name: "Open adjustment" }));
  return await screen.findByRole("dialog", { name: "Repayment adjustment" });
}

/** Open the write-off drawer through the by-id lookup (cleared first —
 * the field keeps its value across drawer opens within one mount). */
async function openWriteOffDetail(user: ReturnType<typeof userEvent.setup>) {
  const lookup = await screen.findByLabelText("Open write-off by id");
  await user.clear(lookup);
  await user.type(lookup, WO_ID);
  await user.click(screen.getByRole("button", { name: "Open write-off" }));
  return await screen.findByRole("dialog", { name: "Loan write-off" });
}

/** Type the byte-identical phrase and return the danger button. */
async function armDanger(
  user: ReturnType<typeof userEvent.setup>,
  confirmLabel: string,
  phrase: string,
) {
  await user.type(await screen.findByLabelText(`Type "${phrase}" to confirm`), phrase);
  return screen.getByRole("button", { name: confirmLabel });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  clearWriteOffMakers();
  clearVotedWriteOffs();
  grantPermissions(FULL_PERMS);
  mocked.fetchAdjustment.mockResolvedValue(pendingAdjustment());
  // Registers default EMPTY: the register tests seed their own pages.
  mocked.fetchAdjustmentsPage.mockResolvedValue({ items: [], nextCursor: null });
  mocked.fetchWriteOffsPage.mockResolvedValue({ items: [], nextCursor: null });
  mocked.requestAdjustment.mockResolvedValue(pendingAdjustment());
  mocked.approveAdjustment.mockResolvedValue(ADJUSTMENT_RESULT);
  mocked.rejectAdjustment.mockResolvedValue(
    pendingAdjustment({ status: "rejected", checker_id: ADMIN_ID, version: 4 }),
  );
  mocked.fetchWriteOff.mockResolvedValue(requestedWriteOff());
  mocked.requestWriteOff.mockResolvedValue(requestedWriteOff());
  mocked.voteOnWriteOff.mockResolvedValue(VOTE_TALLY_OPEN);
  mocked.voidWriteOff.mockResolvedValue(requestedWriteOff({ status: "rejected", version: 3 }));
  mocked.postWriteOff.mockResolvedValue(WRITE_OFF_POST_RESULT);
  mocked.recordRecoveryReceipt.mockResolvedValue(RECEIPT_RESULT);
  mocked.fetchRecoveryReceiptsPage.mockResolvedValue(EMPTY_RECEIPTS_PAGE);
  mocked.postFee.mockResolvedValue(FEE_RESULT);
  mockedMembers.fetchMembersPage.mockResolvedValue({ items: [MEMBER], nextCursor: null });
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
});

afterEach(() => {
  clearSession();
});

test("lookup fields validate the UUID shape BEFORE any fetch — garbage never leaves the screen", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.type(await screen.findByLabelText("Review adjustment by id"), "not-a-uuid");
  await user.click(screen.getByRole("button", { name: "Open adjustment" }));
  expect(await screen.findByText(/full adjustment UUID/)).toBeInTheDocument();
  expect(mocked.fetchAdjustment).not.toHaveBeenCalled();

  await user.type(screen.getByLabelText("Open write-off by id"), "also-garbage");
  await user.click(screen.getByRole("button", { name: "Open write-off" }));
  expect(await screen.findByText(/full write-off UUID/)).toBeInTheDocument();
  expect(mocked.fetchWriteOff).not.toHaveBeenCalled();
});

test("hostile reason renders as inert TEXT in the adjustment drawer (named XSS threat)", async () => {
  const user = userEvent.setup();
  mocked.fetchAdjustment.mockResolvedValue(pendingAdjustment({ reason: HOSTILE_REASON }));
  const { container } = mountScreen();

  await openAdjustmentDetail(user);
  expect(await screen.findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("MONEY (blocker (a)): the adjustment drawer renders every SERVER figure VERBATIM — the component sum (450.10) is NEVER derived or rendered", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openAdjustmentDetail(user);
  expect(await within(drawer).findByText("KES 500.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 100.10")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 150.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 200.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 40,000.00")).toBeInTheDocument();
  // Falsifiable: verify-the-identity client code would render 450.10.
  expect(within(drawer).queryByText("KES 450.10")).toBeNull();
  expect(within(drawer).queryByText(/Totals/)).toBeNull();
});

test("MONEY (blocker (a)): the write-off drawer renders the write-once snapshot VERBATIM — balance+penalty_due (41,500.10) is NEVER summed client-side", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openWriteOffDetail(user);
  expect(await within(drawer).findByText("KES 40,000.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 1,500.10")).toBeInTheDocument();
  expect(within(drawer).getAllByText("KES 77,777.77").length).toBeGreaterThan(0);
  expect(within(drawer).queryByText("KES 41,500.10")).toBeNull();
});

test("deny-by-default: a view-only role gets NO maker affordances and NO checker controls in either drawer — zero write calls possible", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("Repayment adjustments");
  expect(screen.queryByRole("button", { name: "+ Request adjustment" })).toBeNull();
  expect(screen.queryByRole("button", { name: "+ Post fee" })).toBeNull();
  expect(screen.queryByRole("button", { name: "+ Request write-off" })).toBeNull();
  // Structural: the stripped role never even fetches the member picker.
  expect(mockedMembers.fetchMembersPage).not.toHaveBeenCalled();

  const adjDrawer = await openAdjustmentDetail(user);
  expect(
    await within(adjDrawer).findByText(/no corrections approval permission/),
  ).toBeInTheDocument();
  expect(within(adjDrawer).queryByRole("button", { name: /Approve & post reversal/ })).toBeNull();
  expect(within(adjDrawer).queryByRole("button", { name: /Reject adjustment/ })).toBeNull();
  await user.click(within(adjDrawer).getByRole("button", { name: "Close" }));

  const woDrawer = await openWriteOffDetail(user);
  expect(
    await within(woDrawer).findByText(/no corrections approval permission/),
  ).toBeInTheDocument();
  expect(within(woDrawer).queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(within(woDrawer).queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(within(woDrawer).queryByRole("button", { name: /Void write-off/ })).toBeNull();

  expect(mocked.approveAdjustment).not.toHaveBeenCalled();
  expect(mocked.rejectAdjustment).not.toHaveBeenCalled();
  expect(mocked.voteOnWriteOff).not.toHaveBeenCalled();
  expect(mocked.voidWriteOff).not.toHaveBeenCalled();
  expect(mocked.postWriteOff).not.toHaveBeenCalled();
});

test("SoD SERVER TRUTH (the !70 pattern): a maker_id matching the signed-in checker withholds the adjustment checker controls STRUCTURALLY — no registry involved", async () => {
  const user = userEvent.setup();
  mocked.fetchAdjustment.mockResolvedValue(pendingAdjustment({ maker_id: ADMIN_ID }));
  mountScreen();

  await openAdjustmentDetail(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  expect(screen.getByText(/You requested this adjustment \(server attribution\)/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Approve & post reversal/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Reject adjustment/ })).toBeNull();
  expect(mocked.approveAdjustment).not.toHaveBeenCalled();
});

test("attribution (the !70 pattern): maker/checker render as the bare-UUID SHORT id with the full UUID on title; NULL checker is the honest '— (undecided)'; NO directory record is ever fetched", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openAdjustmentDetail(user);
  await within(drawer).findByText("Record version");
  const makerCell = within(drawer).getAllByTitle(OTHER_OFFICER_ID)[0];
  expect(makerCell).toHaveTextContent(OTHER_OFFICER_ID.slice(0, 8));
  expect(within(drawer).getByText("— (undecided)")).toBeInTheDocument();
  // Least disclosure: the staff UUID is NEVER resolved to a person.
  expect(mockedMembers.fetchMember).not.toHaveBeenCalled();
  expect(within(drawer).queryByText(/@/)).toBeNull();
});

test("APPROVE: typed confirmation starts DISARMED; a triple-clicked confirm posts exactly ONE approval; the result panel renders the SERVER reversal verbatim; the affordance is SPENT", async () => {
  const user = userEvent.setup();
  let release: (value: typeof ADJUSTMENT_RESULT) => void = () => undefined;
  mocked.approveAdjustment.mockImplementation(
    () =>
      new Promise<typeof ADJUSTMENT_RESULT>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openAdjustmentDetail(user);
  await user.click(await screen.findByRole("button", { name: "Approve & post reversal…" }));
  const disarmed = screen.getByRole("button", { name: "Approve & post reversal" });
  expect(disarmed).toBeDisabled();
  expect(mocked.approveAdjustment).not.toHaveBeenCalled();

  const confirm = await armDanger(user, "Approve & post reversal", ADJ_PHRASE);
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.approveAdjustment).toHaveBeenCalledTimes(1);
  expect(mocked.approveAdjustment.mock.calls[0]?.[0]).toBe(ADJ_ID);

  release(ADJUSTMENT_RESULT);
  expect(await screen.findByText(/Reversal posted · ref ADJ-0001/)).toBeInTheDocument();
  expect(screen.getByText("KES 40,500.00")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve & post reversal…" })).toBeNull();
  expect(mocked.approveAdjustment).toHaveBeenCalledTimes(1);
});

test("APPROVE 409 (snapshot drift, NOTHING posted): EXACTLY ONE attempt; explicit reload refetches and NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.approveAdjustment
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-drift"))
    .mockResolvedValue(ADJUSTMENT_RESULT);
  mountScreen();

  await openAdjustmentDetail(user);
  const readsBefore = mocked.fetchAdjustment.mock.calls.length;
  await user.click(await screen.findByRole("button", { name: "Approve & post reversal…" }));
  await user.click(await armDanger(user, "Approve & post reversal", ADJ_PHRASE));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.approveAdjustment).toHaveBeenCalledTimes(1);
  const staleKey = mocked.approveAdjustment.mock.calls[0]?.[1];

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchAdjustment.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(await screen.findByText(/reject this adjustment and raise a fresh one/)).toBeInTheDocument();
  expect(mocked.approveAdjustment).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Approve & post reversal…" }));
  await user.click(await armDanger(user, "Approve & post reversal", ADJ_PHRASE));
  await waitFor(() => expect(mocked.approveAdjustment).toHaveBeenCalledTimes(2));
  expect(mocked.approveAdjustment.mock.calls[1]?.[1]).not.toBe(staleKey);
});

test("REJECT: the rationale is REQUIRED before arming (!52 F2); the confirmed reject posts ONCE pinning the FRESH record version", async () => {
  const user = userEvent.setup();
  mountScreen();

  await openAdjustmentDetail(user);
  await user.click(await screen.findByRole("button", { name: "Reject adjustment…" }));
  expect(await screen.findByText(/rationale is required/)).toBeInTheDocument();
  expect(mocked.rejectAdjustment).not.toHaveBeenCalled();

  await user.type(
    screen.getByLabelText("Rejection rationale (required to reject)"),
    "Figures drifted",
  );
  await user.click(screen.getByRole("button", { name: "Reject adjustment…" }));
  const confirm = await armDanger(user, "Reject adjustment", ADJ_PHRASE);
  await user.click(confirm);
  await user.click(confirm);

  await waitFor(() => expect(mocked.rejectAdjustment).toHaveBeenCalledTimes(1));
  expect(mocked.rejectAdjustment.mock.calls[0]?.[0]).toBe(ADJ_ID);
  expect(mocked.rejectAdjustment.mock.calls[0]?.[1]).toBe(3);
  expect(mocked.rejectAdjustment.mock.calls[0]?.[2]).toBe("Figures drifted");
});

test("TERMINAL adjustment statuses structurally WITHDRAW the checker panel (posted AND rejected)", async () => {
  const user = userEvent.setup();
  mocked.fetchAdjustment.mockResolvedValue(
    pendingAdjustment({
      status: "posted",
      checker_id: OTHER_OFFICER_ID,
      reversal_transaction_id: "55555555-6666-7777-8888-999999999999",
      decided_at: "2026-08-02T10:00:00+00:00",
    }),
  );
  const view = mountScreen();

  await openAdjustmentDetail(user);
  expect(await screen.findByText(/terminal\s+state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Approve & post reversal/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /Reject adjustment/ })).toBeNull();

  view.unmount();
  mocked.fetchAdjustment.mockResolvedValue(pendingAdjustment({ status: "rejected" }));
  mountScreen();
  await openAdjustmentDetail(user);
  expect(await screen.findByText(/terminal\s+state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Approve & post reversal/ })).toBeNull();
  expect(mocked.approveAdjustment).not.toHaveBeenCalled();
  expect(mocked.rejectAdjustment).not.toHaveBeenCalled();
});

test("WRITE-OFF SoD (registry fallback — the !70 pattern, honestly): the witnessed requester gets NO vote/post controls; an UNWITNESSED record mounts them with the honest label", async () => {
  const user = userEvent.setup();
  recordWriteOffMaker(WO_ID, ADMIN_ID);
  const view = mountScreen();

  await openWriteOffDetail(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  expect(screen.getByText(/You requested this write-off \(witnessed by this tab\)/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(mocked.voteOnWriteOff).not.toHaveBeenCalled();

  // The UNWITNESSED leg: the honest label states the contract gap and
  // that the server still bans the requester — controls DO mount.
  view.unmount();
  clearWriteOffMakers();
  expect(writeOffMakerOf(WO_ID)).toBe(WRITE_OFF_MAKER_UNKNOWN);
  mountScreen();
  await openWriteOffDetail(user);
  expect(
    await screen.findByText(/not on the record .*does not attribute the requester/),
  ).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Vote approve" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeInTheDocument();
});

test("VOTE: triple-clicked confirm records exactly ONE vote; the tally renders SERVER counts; the affordance stays SPENT across remounts (W58-6)", async () => {
  const user = userEvent.setup();
  recordWriteOffMaker(WO_ID, OTHER_OFFICER_ID);
  const view = mountScreen();

  await openWriteOffDetail(user);
  await user.click(await screen.findByRole("button", { name: "Vote approve" }));
  const disarmed = screen.getByRole("button", { name: "Confirm approve vote" });
  expect(disarmed).toBeDisabled();
  expect(mocked.voteOnWriteOff).not.toHaveBeenCalled();

  const confirm = await armDanger(user, "Confirm approve vote", WO_PHRASE);
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.voteOnWriteOff).toHaveBeenCalledTimes(1);
  expect(mocked.voteOnWriteOff.mock.calls[0]?.[0]).toBe(WO_ID);
  expect(mocked.voteOnWriteOff.mock.calls[0]?.[1]).toBe("approve");

  expect(await screen.findByText("1 approve")).toBeInTheDocument();
  expect(screen.getByText("0 reject")).toBeInTheDocument();
  expect(screen.getByText("Awaiting quorum")).toBeInTheDocument();
  expect(hasVotedOnWriteOff(WO_ID)).toBe(true);

  // Full remount: component state is gone — the per-tab registry keeps
  // the vote spent regardless.
  view.unmount();
  mountScreen();
  await openWriteOffDetail(user);
  expect(await screen.findByText(/This tab already recorded your vote/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeDisabled();
  expect(mocked.voteOnWriteOff).toHaveBeenCalledTimes(1);
});

test("POST WRITE-OFF (F-M1 + single write): an APPROVED record posts exactly ONCE through the typed confirmation; the result panel renders the SERVER outcome; a 403 renders the least-disclosure banner with the session intact", async () => {
  const user = userEvent.setup();
  recordWriteOffMaker(WO_ID, OTHER_OFFICER_ID);
  mocked.fetchWriteOff.mockResolvedValue(
    requestedWriteOff({ status: "approved", decided_at: "2026-08-02T10:00:00+00:00", version: 3 }),
  );
  let release: (value: typeof WRITE_OFF_POST_RESULT) => void = () => undefined;
  mocked.postWriteOff.mockImplementation(
    () =>
      new Promise<typeof WRITE_OFF_POST_RESULT>((resolve) => {
        release = resolve;
      }),
  );
  const view = mountScreen();

  await openWriteOffDetail(user);
  await user.click(await screen.findByRole("button", { name: "Post write-off…" }));
  const confirm = await armDanger(user, "Post write-off", WO_PHRASE);
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.postWriteOff).toHaveBeenCalledTimes(1);
  expect(mocked.postWriteOff.mock.calls[0]?.[0]).toBe(WO_ID);

  release(WRITE_OFF_POST_RESULT);
  expect(await screen.findByText(/Write-off posted · ref WO-0001/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Post write-off…" })).toBeNull();
  expect(mocked.postWriteOff).toHaveBeenCalledTimes(1);

  // The 403 leg: sanitized banner, no raw category, session intact.
  view.unmount();
  mocked.postWriteOff.mockRejectedValue(new ApiError(403, "forbidden", "corr-403"));
  mountScreen();
  await openWriteOffDetail(user);
  await user.click(await screen.findByRole("button", { name: "Post write-off…" }));
  await user.click(await armDanger(user, "Post write-off", WO_PHRASE));
  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-403/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(hasSession()).toBe(true);
});

test("RECEIPTS: the trail read fires ONLY for a POSTED record; the receipt dialog sends the typed decimal STRING end-to-end; an over-recovery 409 gets ONE attempt and the explicit reload", async () => {
  const user = userEvent.setup();
  mountScreen();

  // Requested record: the trail read NEVER fires.
  await openWriteOffDetail(user);
  expect(await screen.findByText(/Awaiting committee decision/)).toBeInTheDocument();
  expect(mocked.fetchRecoveryReceiptsPage).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Close" }));

  // Posted record: the trail renders the SERVER position verbatim.
  mocked.fetchWriteOff.mockResolvedValue(postedWriteOff());
  const drawer = await openWriteOffDetail(user);
  await waitFor(() => expect(mocked.fetchRecoveryReceiptsPage).toHaveBeenCalled());
  expect(await within(drawer).findByText("KES 0.00")).toBeInTheDocument();

  // Record a receipt: typed decimal STRING end-to-end (blocker (a)).
  mocked.recordRecoveryReceipt.mockRejectedValueOnce(new ApiError(409, "conflict", "corr-over"));
  await user.click(within(drawer).getByRole("button", { name: "Record recovery receipt…" }));
  const dialog = await screen.findByRole("dialog", { name: "Record recovery receipt" });
  await user.type(within(dialog).getByLabelText("Amount received (KES)"), "5000.50");
  await user.selectOptions(within(dialog).getByLabelText("Channel"), "mpesa");
  await user.click(within(dialog).getByRole("button", { name: "Record receipt…" }));
  const confirm = await armDanger(user, "Record receipt", WO_PHRASE);
  await user.click(confirm);
  await user.click(confirm);

  // The over-recovery 409: ONE attempt, the explicit reload flow, and
  // the amount travelled as the byte-identical STRING.
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.recordRecoveryReceipt).toHaveBeenCalledTimes(1);
  expect(mocked.recordRecoveryReceipt.mock.calls[0]?.[0]).toBe(WO_ID);
  expect(mocked.recordRecoveryReceipt.mock.calls[0]?.[1]).toBe("5000.50");
  expect(mocked.recordRecoveryReceipt.mock.calls[0]?.[2]).toBe("mpesa");
  const staleKey = mocked.recordRecoveryReceipt.mock.calls[0]?.[3];

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/receipt may exceed the outstanding claim/),
  ).toBeInTheDocument();
  expect(mocked.recordRecoveryReceipt).toHaveBeenCalledTimes(1);

  // Re-entered identical intent after the acknowledged conflict: a NEW
  // key at the boundary (!60 F3).
  await user.click(within(dialog).getByRole("button", { name: "Record receipt…" }));
  await user.click(await armDanger(user, "Record receipt", WO_PHRASE));
  await waitFor(() => expect(mocked.recordRecoveryReceipt).toHaveBeenCalledTimes(2));
  expect(mocked.recordRecoveryReceipt.mock.calls[1]?.[3]).not.toBe(staleKey);
  expect(await screen.findByText(/Receipt posted · ref RC-0001/)).toBeInTheDocument();
  expect(screen.getByText("KES 72,777.27")).toBeInTheDocument();
});

test("FEE: NO amount field exists anywhere (FM5/v1.1 rule 1); the result renders the SERVER-resolved figure; an EXITED member is structurally withdrawn", async () => {
  const user = userEvent.setup();
  const view = mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Post fee" }));
  const drawer = await screen.findByRole("dialog", { name: "Post misc fee" });
  // The one thing this drawer must never offer: an amount entry.
  expect(within(drawer).queryByLabelText(new RegExp("amount", "i"))).toBeNull();

  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  await within(drawer).findByText(/Member verified: Jane Wanjiku · M-0001/);
  await user.selectOptions(within(drawer).getByLabelText("Channel"), "mpesa");
  await user.click(within(drawer).getByRole("button", { name: "Post fee…" }));
  const confirm = await armDanger(user, "Post fee", "M-0001");
  await user.click(confirm);
  await user.click(confirm);

  await waitFor(() => expect(mocked.postFee).toHaveBeenCalledTimes(1));
  expect(mocked.postFee.mock.calls[0]?.[0]).toBe(MEMBER_ID);
  expect(mocked.postFee.mock.calls[0]?.[1]).toBe("registration");
  expect(mocked.postFee.mock.calls[0]?.[2]).toBe("mpesa");
  // The SERVER's P13.7-configured figure renders from the response.
  expect(await screen.findByText(/Registration fee posted · ref FE-0001/)).toBeInTheDocument();
  expect(screen.getByText("KES 1,000.00")).toBeInTheDocument();

  // The exited-member leg (fresh mount — a fresh record read): the
  // posting affordance is structurally withdrawn.
  view.unmount();
  mockedMembers.fetchMember.mockResolvedValue({ ...MEMBER, status: "exited" });
  mountScreen();
  await user.click(await screen.findByRole("button", { name: "+ Post fee" }));
  const drawer2 = await screen.findByRole("dialog", { name: "Post misc fee" });
  await user.selectOptions(await within(drawer2).findByLabelText("Member"), MEMBER_ID);
  expect(await within(drawer2).findByText(/This member has EXITED/)).toBeInTheDocument();
  expect(within(drawer2).getByRole("button", { name: "Post fee…" })).toBeDisabled();
  expect(mocked.postFee).toHaveBeenCalledTimes(1);
});

test("MAKER REQUEST (adjustment): one write per intent; the result panel renders the snapshot verbatim and points the checker at the (a).1 register", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Request adjustment" }));
  const drawer = await screen.findByRole("dialog", { name: "Request repayment adjustment" });
  await user.type(within(drawer).getByLabelText("Repayment id"), REPAYMENT_ID);
  await user.type(within(drawer).getByLabelText("Reason"), "Duplicate repayment posting");
  await user.click(within(drawer).getByRole("button", { name: "Request adjustment…" }));
  const confirm = await armDanger(user, "Request adjustment", REPAYMENT_ID.slice(0, 8));
  await user.click(confirm);
  await user.click(confirm);

  await waitFor(() => expect(mocked.requestAdjustment).toHaveBeenCalledTimes(1));
  expect(mocked.requestAdjustment.mock.calls[0]?.[0]).toBe(REPAYMENT_ID);
  expect(await screen.findByText(/Adjustment requested · awaiting a distinct checker/)).toBeInTheDocument();
  // The record id renders, and the panel points the checker at the
  // register the (a).1 contract expansion delivered.
  expect(within(drawer).getByText(ADJ_ID)).toBeInTheDocument();
  expect(
    within(drawer).getByText(/leads the pending-adjustments\s+checker register/),
  ).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Request adjustment…" })).toBeNull();
});

test("MAKER REQUEST (write-off): success records the per-tab SoD witness for THIS tab", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Request write-off" }));
  const drawer = await screen.findByRole("dialog", { name: "Request loan write-off" });
  await user.type(within(drawer).getByLabelText("Loan id"), LOAN_ID);
  await user.type(within(drawer).getByLabelText("Reason"), "Member absconded");
  await user.click(within(drawer).getByRole("button", { name: "Request write-off…" }));
  await user.click(await armDanger(user, "Request write-off", LOAN_ID.slice(0, 8)));

  await waitFor(() => expect(mocked.requestWriteOff).toHaveBeenCalledTimes(1));
  expect(await screen.findByText(/Write-off requested · awaiting committee votes/)).toBeInTheDocument();
  // The SoD witness: this tab knows its own request (F-M4).
  expect(writeOffMakerOf(WO_ID)).toBe(ADMIN_ID);
});

test("boundary REJECTS hostile shapes (real Zod, unmocked): numeric money, unknown status, garbage classification can never reach a drawer", () => {
  expect(
    adjustmentSchema.safeParse({ ...pendingAdjustment(), amount: 500 }).success,
  ).toBe(false);
  expect(
    adjustmentSchema.safeParse({ ...pendingAdjustment(), status: "escalated" }).success,
  ).toBe(false);
  expect(
    writeOffSchema.safeParse({ ...requestedWriteOff(), total_written_off: "<b>77</b>" }).success,
  ).toBe(false);
  expect(
    writeOffSchema.safeParse({ ...requestedWriteOff(), classification: "normal" }).success,
  ).toBe(false);
});

test("query-path 401 tears the session down AND empties BOTH per-tab registries (W58-2): the next operator inherits no witness and no spent votes", async () => {
  const user = userEvent.setup();
  recordWriteOffMaker(WO_ID, ADMIN_ID);
  recordVotedWriteOff(WO_ID);
  mocked.fetchAdjustment.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await openAdjustmentDetail(user);
  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
  expect(writeOffMakerOf(WO_ID)).toBe(WRITE_OFF_MAKER_UNKNOWN);
  expect(hasVotedOnWriteOff(WO_ID)).toBe(false);
});

test("REGISTERS (ledger (a).1/(a).2): rows render in the SERVER page order with VERBATIM money — the component sums (450.10 / 41,500.10) never render; a row click opens the matching drawer", async () => {
  const user = userEvent.setup();
  const secondAdj = pendingAdjustment({
    id: "aaaaaaaa-2222-3333-4444-555555555555",
    status: "posted",
    checker_id: OTHER_OFFICER_ID,
    amount: "999.99",

});
// The SERVER's pending-first order: pending leads, posted trails —
// rendered EXACTLY as served (never re-sorted locally).
mocked.fetchAdjustmentsPage.mockResolvedValue({
    items: [pendingAdjustment(), secondAdj],
    nextCursor: null,

});
mocked.fetchWriteOffsPage.mockResolvedValue({
    items: [requestedWriteOff()],
    nextCursor: null,

});
mountScreen();

// Adjustments register: server order preserved row-for-row.
const adjRows = await screen.findAllByRole("button", { name: new RegExp("KES 500.00") });
expect(adjRows.length).toBeGreaterThan(0);
const tables = screen.getAllByRole("region", { name: "Table" });
const adjTable = tables[0]!;
const rows = within(adjTable).getAllByRole("button");
expect(rows[0]).toHaveTextContent("Pending approval");
expect(rows[0]).toHaveTextContent("KES 500.00");
expect(rows[1]).toHaveTextContent("Posted");
expect(rows[1]).toHaveTextContent("KES 999.99");
// Non-additive fixture (blocker (a)): the component sum is nowhere.
expect(screen.queryByText("KES 450.10")).toBeNull();

// Write-off register: snapshot figure verbatim, never summed.
expect(screen.getByText("KES 77,777.77")).toBeInTheDocument();
expect(screen.queryByText("KES 41,500.10")).toBeNull();
// Maker attribution: bare-UUID short id (!70 render), full on title.
expect(within(adjTable).getAllByTitle(OTHER_OFFICER_ID)[0]).toHaveTextContent(
    OTHER_OFFICER_ID.slice(0, 8),
);

// Row click drills into the SAME detail drawer the lookup opens.
await user.click(rows[0]!);
expect(await screen.findByRole("dialog", { name: "Repayment adjustment" })).toBeInTheDocument();
expect(mocked.fetchAdjustment).toHaveBeenCalledWith(ADJ_ID);
});

test("REGISTERS: empty pages render the honest empty message; a failed page renders the shared error alert — never a fabricated register", async () => {
  mocked.fetchWriteOffsPage.mockRejectedValue(new ApiError(500, "internal_error", "corr-reg"));
  mountScreen();

  expect(
    await screen.findByText("No repayment adjustments yet — nothing awaits a checker."),
).toBeInTheDocument();
  // The Providers default retries queries ONCE (~1s backoff) before
  // surfacing the error state — allow for it (the house pattern; this
  // leg flaked at the default 1s timeout on a loaded runner).
  expect(await screen.findByRole("alert", undefined, { timeout: 5000 })).toHaveTextContent(
    /Could not load this list/,
  );
  expect(screen.getByText(/ref corr-reg/)).toBeInTheDocument();

});

test("W56-3: a stray overlay click never discards the half-completed correction entry or the armed money drawers", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Request adjustment" }));
  const drawer = await screen.findByRole("dialog", { name: "Request repayment adjustment" });
  await user.type(within(drawer).getByLabelText("Repayment id"), REPAYMENT_ID);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Request repayment adjustment" })).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Repayment id")).toHaveValue(REPAYMENT_ID);
  await user.click(within(drawer).getByRole("button", { name: "Close" }));

  await openWriteOffDetail(user);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Loan write-off" })).toBeInTheDocument();
});
