/**
 * Member-exit register + lifecycle-write adversarial tests THROUGH THE
 * REAL SCREEN CODE (P15 module 7 — the most money-critical surface of
 * the batch: the TERMINAL settlement). The feature API layers are
 * stubbed at the module boundary; rendering, keyset paging, permission
 * gating, idempotency-key custody, the fresh reads, SoD structural
 * withholding, 409 handling and the typed confirmations are the real
 * shipped code, mounted under the REAL <Providers>.
 *
 * Centrepieces:
 * - EVERY write (request, vote, void, settle) has a triple-click
 *   single-write proof and a typed ConfirmDangerModal (W58-3 — exit
 *   votes decide money movement).
 * - F-M1: the settle mutate path STRUCTURALLY refuses to fire without
 *   the loaded fresh record; the pinned wire version is always the
 *   fresh read's (never a sentinel 0).
 * - F-M4: MakerCheckerPanel SoD — the witnessed initiator gets NO
 *   settle/vote controls (structurally withheld, zero writes
 *   possible); the UNKNOWN maker renders controls with the honest
 *   label; the server 403 renders the least-disclosure banner.
 * - F-M5: votes are SPENT across remounts (votedRegistry, W58-6) and
 *   BOTH per-tab registries die on the query-path 401 AND explicit
 *   sign-out (registerSessionScopedStore — W58-2).
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { logout } from "@/modules/auth/api";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { ExitsScreen } from "../components/ExitsScreen";
import { SettleExitDialog } from "../components/SettleExitDialog";
import {
  clearExitMakers,
  EXIT_MAKER_UNKNOWN,
  exitMakerOf,
  recordExitMaker,
} from "../makerRegistry";
import { clearVotedExits, hasVotedOnExit, recordVotedExit } from "../votedRegistry";
import { exitSchema, type ExitRecord, type ExitStatement, type ExitVoteResult } from "../schemas";
import * as exitsApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchExitsPage: jest.fn(),
    fetchExit: jest.fn(),
    fetchExitEligibility: jest.fn(),
    createExitRequest: jest.fn(),
    voteOnExit: jest.fn(),
    voidExit: jest.fn(),
    postExitSettlement: jest.fn(),
    fetchExitStatement: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(exitsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const OTHER_OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXIT_ID = "eeeeeeee-aaaa-bbbb-cccc-444444444444";
const NEGATIVE_EXIT_ID = "ffffffff-aaaa-bbbb-cccc-444444444444";

/** ConfirmDangerModal phrase for the lifecycle writes (record id prefix). */
const PHRASE = EXIT_ID.slice(0, 8);

// Attacker-influenced free-text fields on this module — must render as
// inert text (named XSS threat).
const HOSTILE_REASON = '<img src=x onerror=window.__pwned=1> "quoted" reason';
const HOSTILE_NAME = "<script>window.__pwned=2</script> Jane";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(claims: object): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ ...claims, exp })}.sig`;
}

function requestedExit(overrides: Partial<ExitRecord> = {}): ExitRecord {
  return {
    id: EXIT_ID,
    member_id: MEMBER_ID,
    status: "requested",
    reason: "Relocation",
    requested_by: null,
    shares_amount: "25000.10",
    deposits_amount: "150000.10",
    loan_balance: "40000.00",
    fees: "500.00",
    net_payable: "134500.20",
    decided_at: null,
    settled_at: null,
    settlement_txn_id: null,
    version: 7,
    created_at: "2026-08-01T09:00:00+00:00",
    ...overrides,
  };
}

function approvedExit(overrides: Partial<ExitRecord> = {}): ExitRecord {
  return requestedExit({
    status: "approved",
    decided_at: "2026-08-02T10:00:00+00:00",
    ...overrides,
  });
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

/** The canonical statement document. equity is DELIBERATELY not the
 * sum of shares+deposits: the screen must render the SERVER's line
 * verbatim — a client-side re-derivation would fail these tests. */
const STATEMENT: ExitStatement = {
  exit_id: EXIT_ID,
  member_id: MEMBER_ID,
  member_no: "M-0001",
  member_name: "Jane Wanjiku",
  member_status: "active",
  exit_status: "requested",
  reason: "Relocation",
  requested_by: null,
  shares_amount: "25000.10",
  deposits_amount: "150000.10",
  equity: "999999.99",
  loan_balance: "40000.00",
  fees: "500.00",
  net_payable: "-4000.00",
  requested_at: "2026-08-01T09:00:00+00:00",
  decided_at: null,
  settled_at: null,
  settlement_txn_ref: null,
};

const TALLY_OPEN: ExitVoteResult = {
  approvals: 1,
  rejections: 0,
  decision: null,
  status: "requested",
};

const SETTLEMENT_RESULT = {
  exit: approvedExit({
    status: "settled",
    settled_at: "2026-08-03T10:00:00+00:00",
    settlement_txn_id: "dddddddd-1111-2222-3333-444444444444",
    version: 8,
  }),
  txn_ref: "EX-0001",
};

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

/** members:view ONLY — no edit (no request), no approve (no vote/void/settle). */
const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
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
      <ExitsScreen />
    </Providers>,
  );
}

/** Open the detail drawer by clicking the (only) register row. */
async function openDetail(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("KES 134,500.20"));
  return await screen.findByRole("dialog", { name: "Exit request" });
}

/** Type the byte-identical phrase into the (unique) typed-confirmation
 * field and return the danger confirm button by its EXACT label. */
async function armDanger(
  user: ReturnType<typeof userEvent.setup>,
  confirmLabel: string,
  phrase: string = PHRASE,
) {
  await user.type(await screen.findByLabelText(`Type "${phrase}" to confirm`), phrase);
  return screen.getByRole("button", { name: confirmLabel });
}

/** Walk detail drawer → settle dialog for an APPROVED record. */
async function openSettleDialog(user: ReturnType<typeof userEvent.setup>) {
  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Post settlement…" }));
  await screen.findByText("Pinned record version");
}

/** Arm and fire one settlement attempt for the chosen channel. */
async function attemptSettlement(user: ReturnType<typeof userEvent.setup>, channel = "mpesa") {
  await user.selectOptions(await screen.findByLabelText("Payout channel"), channel);
  await user.click(screen.getByRole("button", { name: "Post settlement…" }));
  await user.click(await armDanger(user, "Post settlement"));
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt({ sub: ADMIN_ID }), refreshToken: "refresh-1" });
  clearExitMakers();
  clearVotedExits();
  grantPermissions(FULL_PERMS);
  mocked.fetchExitsPage.mockResolvedValue({ items: [requestedExit()], nextCursor: null });
  mocked.fetchExit.mockResolvedValue(requestedExit());
  mocked.createExitRequest.mockResolvedValue(requestedExit());
  mocked.voteOnExit.mockResolvedValue(TALLY_OPEN);
  mocked.voidExit.mockResolvedValue(requestedExit({ status: "rejected", version: 8 }));
  mocked.postExitSettlement.mockResolvedValue(SETTLEMENT_RESULT);
  mocked.fetchExitStatement.mockResolvedValue(STATEMENT);
  mockedMembers.fetchMembersPage.mockResolvedValue(page([MEMBER]));
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
  // U6 advisory checklist default: ELIGIBLE — the dedicated legs
  // (exit-eligibility-screen.test.tsx) cover render/gating/XSS; here it
  // only needs to never block the request flows under test.
  mocked.fetchExitEligibility.mockResolvedValue({
    member_id: MEMBER_ID,
    member_status: "active",
    status_allows_exit: true,
    open_exit_exists: false,
    live_guarantees_count: 0,
    open_applications_count: 0,
    active_loans_count: 0,
    unresolved_writeoff_claim: false,
    advisory_eligible: true,
    as_of: "2026-08-06T12:00:00+00:00",
  });
});

afterEach(() => {
  clearSession();
});

test("register renders every settlement figure VERBATIM in its own labelled column — a NEGATIVE net payable keeps its sign; nothing is netted, summed or totalled (blocker (a))", async () => {
  mocked.fetchExitsPage.mockResolvedValue({
    items: [
      requestedExit(),
      requestedExit({ id: NEGATIVE_EXIT_ID, net_payable: "-4000.00" }),
    ],
    nextCursor: null,
  });
  mountScreen();

  // Trailing cents survive — a numeric round-trip would drop them.
  expect(await screen.findByText("KES 134,500.20")).toBeInTheDocument();
  expect(screen.getAllByText("KES 25,000.10").length).toBeGreaterThan(0);
  expect(screen.getAllByText("KES 150,000.10").length).toBeGreaterThan(0);
  expect(screen.getAllByText("KES 40,000.00").length).toBeGreaterThan(0);
  expect(screen.getAllByText("KES 500.00").length).toBeGreaterThan(0);
  // The documented negative branch renders verbatim WITH its sign.
  expect(screen.getByText("KES -4,000.00")).toBeInTheDocument();
  // No client-side arithmetic anywhere: no shares+deposits sum, no
  // equity minus loan figure, no totals row.
  expect(screen.queryByText("KES 175,000.20")).toBeNull();
  expect(screen.queryByText(/Totals/)).toBeNull();
  expect(screen.getByText(/totals are not computed in this screen/)).toBeInTheDocument();
});

test("keyset paging: Load more follows the server cursor VERBATIM (gate 1.3); the status filter drives the SERVER query", async () => {
  const user = userEvent.setup();
  mocked.fetchExitsPage
    .mockResolvedValueOnce({ items: [requestedExit()], nextCursor: "opaque-cursor-§1" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchExitsPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchExitsPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchExitsPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");

  await user.selectOptions(screen.getByLabelText("Status"), "approved");
  await waitFor(() =>
    expect(mocked.fetchExitsPage).toHaveBeenCalledWith({ status: "approved" }, null),
  );
});

test("deny-by-default: a view-only role gets NO request affordance and ZERO members fetches; vote/void/settle affordances are withheld in the drawers", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 134,500.20");
  expect(screen.queryByRole("button", { name: "+ Request exit" })).toBeNull();
  // Structural: the stripped role never even FETCHES the member picker.
  expect(mockedMembers.fetchMembersPage).not.toHaveBeenCalled();

  await openDetail(user);
  expect(await screen.findByText(/no members approval permission/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Void request…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  // The read affordance (statement) remains — members:view.
  expect(screen.getByRole("button", { name: "View exit statement" })).toBeInTheDocument();
  expect(mocked.voteOnExit).not.toHaveBeenCalled();
  expect(mocked.voidExit).not.toHaveBeenCalled();
  expect(mocked.postExitSettlement).not.toHaveBeenCalled();
});

test("hostile reason and member name render as inert TEXT in the detail drawer (named XSS threat)", async () => {
  const user = userEvent.setup();
  mocked.fetchExit.mockResolvedValue(requestedExit({ reason: HOSTILE_REASON }));
  mockedMembers.fetchMember.mockResolvedValue({ ...MEMBER, name: HOSTILE_NAME });
  const { container } = mountScreen();

  await openDetail(user);
  expect(await screen.findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  expect(await screen.findByText(new RegExp("window.__pwned=2"))).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("the CANONICAL statement renders every figure VERBATIM — the server's equity line (never re-derived), the negative net payable WITH its sign, hostile strings inert, NO download affordance", async () => {
  const user = userEvent.setup();
  mocked.fetchExitStatement.mockResolvedValue({
    ...STATEMENT,
    member_name: HOSTILE_NAME,
    reason: HOSTILE_REASON,
    settlement_txn_ref: "<b>EX-777</b>",
  });
  const { container } = mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "View exit statement" }));
  const drawer = await screen.findByRole("dialog", { name: "Exit statement" });

  // The SERVER's equity line renders byte-identically even though it
  // does NOT equal shares+deposits in this fixture — falsifiable: a
  // client-side re-derivation would render KES 175,000.20 instead.
  expect(await within(drawer).findByText("KES 999,999.99")).toBeInTheDocument();
  expect(within(drawer).queryByText("KES 175,000.20")).toBeNull();
  // Every other line verbatim; the negative net payable keeps its sign.
  expect(within(drawer).getByText("KES 25,000.10")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 150,000.10")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 40,000.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES 500.00")).toBeInTheDocument();
  expect(within(drawer).getByText("KES -4,000.00")).toBeInTheDocument();

  // Hostile strings inert — no markup materialised.
  expect(within(drawer).getAllByText(new RegExp("window.__pwned")).length).toBeGreaterThan(0);
  expect(within(drawer).getByText(new RegExp("<b>EX-777</b>"))).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("b")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();

  // NO download affordance exists here (the export is the reports
  // module's member_exit_statement — the !63 F-R1 class cannot arise).
  expect(within(drawer).queryByRole("button", { name: /download|csv|pdf/i })).toBeNull();
  expect(within(drawer).getByText(/Reports ▸ Member exit statement/)).toBeInTheDocument();
});

test("hostile money/status shapes are REJECTED by the real boundary and can never render — the register consumes the rejection as an error state", async () => {
  // The REAL (unmocked) Zod boundary rejects hostile money and unknown
  // status vocabulary…
  expect(
    exitSchema.safeParse({ ...requestedExit(), net_payable: "<b>134500.20</b>" }).success,
  ).toBe(false);
  expect(exitSchema.safeParse({ ...requestedExit(), fees: "1e5" }).success).toBe(false);
  expect(exitSchema.safeParse({ ...requestedExit(), status: "escalated" }).success).toBe(false);
  // …so such a page can only reach the screen as a rejected fetch: the
  // register renders the error state, never the garbage.
  mocked.fetchExitsPage.mockRejectedValue(new Error("contract violation at the boundary"));
  mountScreen();
  // The Providers default retries queries ONCE (~1s backoff) before
  // surfacing the error state — allow for it.
  expect(
    await screen.findByText(/Could not load this list/, undefined, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/134,500/)).toBeNull();
});

test("REQUEST EXIT: fresh member read gates arming; triple-clicked confirmation produces exactly ONE write; the result panel renders the snapshot verbatim; the maker is witnessed (SoD)", async () => {
  const user = userEvent.setup();
  let releaseMember: (value: typeof MEMBER) => void = () => undefined;
  mockedMembers.fetchMember.mockImplementation(
    () =>
      new Promise<typeof MEMBER>((resolve) => {
        releaseMember = resolve;
      }),
  );
  let releaseCreate: (value: ExitRecord) => void = () => undefined;
  mocked.createExitRequest.mockImplementation(
    () =>
      new Promise<ExitRecord>((resolve) => {
        releaseCreate = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Request exit" }));
  const drawer = await screen.findByRole("dialog", { name: "Request member exit" });
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);

  // The confirmation CANNOT arm before the fresh member read lands.
  await user.click(within(drawer).getByRole("button", { name: "Request exit…" }));
  expect(
    await within(drawer).findByText(/member record is still being verified/),
  ).toBeInTheDocument();
  expect(mocked.createExitRequest).not.toHaveBeenCalled();

  releaseMember(MEMBER);
  expect(await within(drawer).findByText(/Member verified: Jane Wanjiku · M-0001/)).toBeInTheDocument();
  await user.click(within(drawer).getByRole("button", { name: "Request exit…" }));

  // Typed phrase (the member number), then a triple click — the
  // pending short-circuit yields exactly ONE write.
  const confirm = await armDanger(user, "Request exit", "M-0001");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.createExitRequest).toHaveBeenCalledTimes(1);
  expect(mocked.createExitRequest.mock.calls[0]?.[0]).toBe(MEMBER_ID);
  expect(mocked.createExitRequest.mock.calls[0]?.[1]).toBeNull();

  releaseCreate(requestedExit());

  // The result panel replaces the form (SPENT affordance) with the
  // SERVER's snapshot verbatim…
  expect(await screen.findByText(/Exit requested · awaiting committee decision/)).toBeInTheDocument();
  expect(within(drawer).getByText("KES 134,500.20")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Request exit…" })).toBeNull();
  expect(mocked.createExitRequest).toHaveBeenCalledTimes(1);
  // …and this tab witnessed the maker for the SoD panel (F-M4).
  expect(exitMakerOf(EXIT_ID)).toBe(ADMIN_ID);
});

test("REQUEST EXIT 409 (open exit / loan / guarantees): EXACTLY ONE attempt, explicit reload refetches the member and NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.createExitRequest
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-exit"))
    .mockResolvedValue(requestedExit());
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Request exit" }));
  const drawer = await screen.findByRole("dialog", { name: "Request member exit" });
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  await within(drawer).findByText(/Member verified:/);
  await user.click(within(drawer).getByRole("button", { name: "Request exit…" }));
  await user.click(await armDanger(user, "Request exit", "M-0001"));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(screen.getByText(/already has an open exit request/)).toBeInTheDocument();
  expect(mocked.createExitRequest).toHaveBeenCalledTimes(1);
  const staleKey = mocked.createExitRequest.mock.calls[0]?.[2];
  const memberReadsBefore = mockedMembers.fetchMember.mock.calls.length;

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mockedMembers.fetchMember.mock.calls.length).toBeGreaterThan(memberReadsBefore),
  );
  expect(await screen.findByText(/nothing was replayed/)).toBeInTheDocument();
  expect(mocked.createExitRequest).toHaveBeenCalledTimes(1);

  // Re-entering the IDENTICAL entry is a NEW intent — rotated key.
  await user.click(within(drawer).getByRole("button", { name: "Request exit…" }));
  await user.click(await armDanger(user, "Request exit", "M-0001"));
  await waitFor(() => expect(mocked.createExitRequest).toHaveBeenCalledTimes(2));
  expect(mocked.createExitRequest.mock.calls[1]?.[2]).not.toBe(staleKey);
});

test("VOTE (W58-3): typed confirmation starts DISARMED; a triple-clicked confirm records exactly ONE vote; the tally renders SERVER counts; the affordance is spent", async () => {
  const user = userEvent.setup();
  recordExitMaker(EXIT_ID, OTHER_OFFICER_ID);
  let release: (value: ExitVoteResult) => void = () => undefined;
  mocked.voteOnExit.mockImplementation(
    () =>
      new Promise<ExitVoteResult>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Vote approve" }));
  // Disabled until the byte-identical phrase — a reflex click writes nothing.
  const disarmed = screen.getByRole("button", { name: "Confirm approve vote" });
  expect(disarmed).toBeDisabled();
  expect(mocked.voteOnExit).not.toHaveBeenCalled();

  const confirm = await armDanger(user, "Confirm approve vote");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);
  expect(mocked.voteOnExit.mock.calls[0]?.[0]).toBe(EXIT_ID);
  expect(mocked.voteOnExit.mock.calls[0]?.[1]).toBe("approve");

  release(TALLY_OPEN);
  expect(await screen.findByText("1 approve")).toBeInTheDocument();
  expect(screen.getByText("0 reject")).toBeInTheDocument();
  expect(screen.getByText("Awaiting quorum")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeDisabled();
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);
});

test("VOTE SPENT ACROSS REMOUNTS (W58-6/F-M5): closing and reopening the drawer never re-arms the vote buttons in this tab", async () => {
  const user = userEvent.setup();
  recordExitMaker(EXIT_ID, OTHER_OFFICER_ID);
  const view = mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Vote approve" }));
  await user.click(await armDanger(user, "Confirm approve vote"));
  expect(await screen.findByText("1 approve")).toBeInTheDocument();
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);
  expect(hasVotedOnExit(EXIT_ID)).toBe(true);

  // Full remount: component state (the tally panel) is gone — the
  // per-tab registry keeps the affordance withdrawn regardless.
  view.unmount();
  mountScreen();
  await openDetail(user);
  expect(await screen.findByText(/This tab already recorded your vote/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeDisabled();
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);
});

test("VOTE 409 (already voted / status moved): EXACTLY ONE attempt; the explicit reload refetches the record and NEVER replays the vote", async () => {
  const user = userEvent.setup();
  recordExitMaker(EXIT_ID, OTHER_OFFICER_ID);
  mocked.voteOnExit.mockRejectedValue(new ApiError(409, "conflict", "corr-voted"));
  mountScreen();

  await openDetail(user);
  const readsBefore = mocked.fetchExit.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Vote reject" }));
  await user.click(await armDanger(user, "Confirm reject vote"));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchExit.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(await screen.findByText(/Record reloaded — re-check the exit status/)).toBeInTheDocument();
  expect(mocked.voteOnExit).toHaveBeenCalledTimes(1);
});

test("SoD STRUCTURAL (F-M4a, vote): the witnessed initiator gets NO vote affordances — the panel never mounts checker controls for the maker", async () => {
  const user = userEvent.setup();
  recordExitMaker(EXIT_ID, ADMIN_ID);
  mountScreen();

  await openDetail(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  expect(screen.getByText(/You initiated this exit request/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(mocked.voteOnExit).not.toHaveBeenCalled();
});

test("SERVER TRUTH (issue #30 / !66 follow-up): a requested_by matching the signed-in voter withholds the vote affordances STRUCTURALLY — with NO per-tab witness at all", async () => {
  const user = userEvent.setup();
  // No recordExitMaker call: the registry knows nothing. The CONTRACT's
  // attribution alone establishes the maker — falsifiable: drop the
  // requested_by precedence and the registry's UNKNOWN sentinel would
  // mount the controls.
  mocked.fetchExit.mockResolvedValue(requestedExit({ requested_by: ADMIN_ID }));
  mountScreen();

  await openDetail(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/You initiated this exit request \(server attribution\)/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(mocked.voteOnExit).not.toHaveBeenCalled();
});

test("SERVER TRUTH SUPERSEDES the per-tab witness: a DIFFERENT server-attributed initiator renders the short-id maker label and mounts the controls, even when this tab witnessed the signed-in operator's request", async () => {
  const user = userEvent.setup();
  // The tab witnessed the signed-in operator requesting — but the
  // server record attributes the INITIATOR (the maker-checker record
  // itself) to another principal. Server truth wins; the witness is
  // only the fallback for unattributed rows.
  recordExitMaker(EXIT_ID, ADMIN_ID);
  mocked.fetchExit.mockResolvedValue(requestedExit({ requested_by: OTHER_OFFICER_ID }));
  mountScreen();

  await openDetail(user);
  // Least disclosure (FM-D): the SHORT id only — never a name/email.
  expect(
    await screen.findByText(
      new RegExp(`Initiating officer ${OTHER_OFFICER_ID.slice(0, 8)} \\(server attribution\\)`),
    ),
  ).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Vote approve" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeInTheDocument();
});

test("attribution (issue #30 / !66 follow-up): the detail drawer renders requested_by as the SHORT id with the full UUID on title — and NEVER fetches a directory record for it (least disclosure, FM-D)", async () => {
  const user = userEvent.setup();
  mocked.fetchExit.mockResolvedValue(requestedExit({ requested_by: OTHER_OFFICER_ID }));
  mountScreen();

  await openDetail(user);
  await screen.findByText("Record version");
  // Short-id convention: the 8-char prefix renders; the full UUID rides
  // the title attribute (same treatment as every other opaque id).
  const cell = screen.getByTitle(OTHER_OFFICER_ID);
  expect(cell).toHaveTextContent(OTHER_OFFICER_ID.slice(0, 8));
  // Least disclosure: the staff UUID is NEVER resolved to a person —
  // the only member fetch is the exiting member's, and no name/email
  // for the initiator appears anywhere in the DOM.
  for (const call of mockedMembers.fetchMember.mock.calls) {
    expect(call[0]).not.toBe(OTHER_OFFICER_ID);
  }
  expect(screen.queryByText(/full_name/)).toBeNull();
});

test("attribution NULL leg (FM-B): an unattributed record renders the honest affordance — never an invented actor", async () => {
  const user = userEvent.setup();
  // requested_by is null in the base fixture and the registry is empty.
  mountScreen();

  await openDetail(user);
  await screen.findByText("Record version");
  expect(screen.getByText("— (unattributed)")).toBeInTheDocument();
});

test("attribution XSS inertness: a hostile requested_by string renders as inert TEXT in the UUID cell — no markup materialises", async () => {
  const HOSTILE_REQUESTER = "<img src=x onerror=window.__pwned=3>";
  const user = userEvent.setup();
  mocked.fetchExit.mockResolvedValue(requestedExit({ requested_by: HOSTILE_REQUESTER }));
  const { container } = mountScreen();

  await openDetail(user);
  await screen.findByText("Record version");
  // The sliced 8-char prefix ("<img src") renders as literal text…
  expect(screen.getByTitle(HOSTILE_REQUESTER)).toHaveTextContent("<img src");
  // …and never as an element.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("attribution on the CANONICAL statement document: requested_by renders via the short-id convention; the NULL leg stays the honest line", async () => {
  const user = userEvent.setup();
  mocked.fetchExitStatement.mockResolvedValue({
    ...STATEMENT,
    requested_by: OTHER_OFFICER_ID,
  });
  mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "View exit statement" }));
  const drawer = await screen.findByRole("dialog", { name: "Exit statement" });
  // Short id + full UUID on title; no name/email is ever fetched or
  // rendered for the STAFF actor (member_name is the member's own
  // document line).
  const cell = within(drawer).getByTitle(OTHER_OFFICER_ID);
  expect(cell).toHaveTextContent(OTHER_OFFICER_ID.slice(0, 8));
  for (const call of mockedMembers.fetchMember.mock.calls) {
    expect(call[0]).not.toBe(OTHER_OFFICER_ID);
  }
  expect(within(drawer).queryByText(/full_name/)).toBeNull();
});

test("SoD STRUCTURAL (F-M4a, settle): the witnessed initiator gets NO settle controls in the settlement dialog — zero writes possible", async () => {
  const user = userEvent.setup();
  recordExitMaker(EXIT_ID, ADMIN_ID);
  mocked.fetchExitsPage.mockResolvedValue({ items: [approvedExit()], nextCursor: null });
  mocked.fetchExit.mockResolvedValue(approvedExit());
  mountScreen();

  await openSettleDialog(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  // Structurally withheld: no channel picker, no arm button — not
  // merely disabled (MakerCheckerPanel has no override prop).
  expect(screen.queryByLabelText("Payout channel")).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  expect(mocked.postExitSettlement).not.toHaveBeenCalled();
});

test("SoD honesty (F-M4b): an UNWITNESSED maker renders the settle controls with the honest attribution label; a server 403 renders the least-disclosure banner, session intact", async () => {
  const user = userEvent.setup();
  // NO recordExitMaker call: the initiator is unknown to this tab.
  mocked.fetchExitsPage.mockResolvedValue({ items: [approvedExit()], nextCursor: null });
  mocked.fetchExit.mockResolvedValue(approvedExit());
  mocked.postExitSettlement.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  expect(exitMakerOf(EXIT_ID)).toBe(EXIT_MAKER_UNKNOWN);
  await openSettleDialog(user);
  // The honest label (issue #30 / !66 follow-up): the record is
  // unattributed on the SERVER (requested_by null) and no request was
  // witnessed by this tab — nothing is invented.
  expect(
    await screen.findByText(/unattributed on the server record/),
  ).toBeInTheDocument();
  // Controls DO render (the server remains the enforcer)…
  await attemptSettlement(user);

  // …and the server's 403 renders sanitized: category text + ref only.
  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.postExitSettlement).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("SETTLE (F-M1 + single write): the dialog FRESH-READS the record (staleTime 0 — a second read on top of the drawer's); a triple-clicked confirm posts ONCE with the FRESH version pinned (never a sentinel 0); the affordance is SPENT", async () => {
  const user = userEvent.setup();
  mocked.fetchExitsPage.mockResolvedValue({ items: [approvedExit()], nextCursor: null });
  mocked.fetchExit.mockResolvedValue(approvedExit());
  let release: (value: typeof SETTLEMENT_RESULT) => void = () => undefined;
  mocked.postExitSettlement.mockImplementation(
    () =>
      new Promise<typeof SETTLEMENT_RESULT>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  const readsAfterDrawer = mocked.fetchExit.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Post settlement…" }));
  await screen.findByText("Pinned record version");
  // STALE-READ DOUBLE-POST PREVENTION: the dialog re-fetches the record
  // instead of serving the drawer's cache (record class, staleTime 0).
  await waitFor(() =>
    expect(mocked.fetchExit.mock.calls.length).toBeGreaterThan(readsAfterDrawer),
  );

  await user.selectOptions(await screen.findByLabelText("Payout channel"), "mpesa");
  await user.click(screen.getByRole("button", { name: "Post settlement…" }));
  const confirm = await armDanger(user, "Post settlement");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);

  expect(mocked.postExitSettlement).toHaveBeenCalledTimes(1);
  expect(mocked.postExitSettlement.mock.calls[0]?.[0]).toBe(EXIT_ID);
  // The pinned version is the FRESH read's — never 0/null (F-M1).
  expect(mocked.postExitSettlement.mock.calls[0]?.[1]).toBe(7);
  expect(mocked.postExitSettlement.mock.calls[0]?.[2]).toBe("mpesa");

  release(SETTLEMENT_RESULT);
  expect(await screen.findByText(/Settlement posted · ref EX-0001/)).toBeInTheDocument();
  // The SERVER's net paid renders verbatim (register + dialog + result
  // panel all carry the exact decimal string — never a recomputation).
  expect(screen.getAllByText("KES 134,500.20").length).toBeGreaterThan(0);
  // Spent: the action is replaced by the result panel.
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  expect(mocked.postExitSettlement).toHaveBeenCalledTimes(1);
});

test("SETTLE F-M1 STRUCTURAL: while the fresh read has NOT landed, no settle affordance exists at all — zero writes possible from this dialog", async () => {
  // Mount the dialog directly with a never-resolving fresh read: the
  // loading state carries NO panel, NO channel picker, NO confirm.
  mocked.fetchExit.mockImplementation(() => new Promise<ExitRecord>(() => undefined));
  render(
    <Providers>
      <SettleExitDialog exitId={EXIT_ID} onClose={() => undefined} />
    </Providers>,
  );

  expect(await screen.findByText(/Loading exit record…/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Payout channel")).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement" })).toBeNull();
  expect(mocked.postExitSettlement).not.toHaveBeenCalled();
});

test("SETTLE key custody: STABLE across a pure 5xx retry of one intent; a 409 gets ONE attempt, the explicit reload never replays and ROTATES the key for the re-entered intent (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.fetchExitsPage.mockResolvedValue({ items: [approvedExit()], nextCursor: null });
  mocked.fetchExit.mockResolvedValue(approvedExit());
  mocked.postExitSettlement
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-drift"))
    .mockResolvedValue(SETTLEMENT_RESULT);
  mountScreen();

  await openSettleDialog(user);

  // Attempt 1: 5xx. Attempt 2 (identical intent): the key is STABLE.
  await attemptSettlement(user);
  await waitFor(() => expect(mocked.postExitSettlement).toHaveBeenCalledTimes(1));
  await attemptSettlement(user);
  await waitFor(() => expect(mocked.postExitSettlement).toHaveBeenCalledTimes(2));
  const key1 = mocked.postExitSettlement.mock.calls[0]?.[3];
  expect(mocked.postExitSettlement.mock.calls[1]?.[3]).toBe(key1);

  // Attempt 2 hit the DRIFT 409: one attempt, conflict banner, and the
  // P12 remedy stated to the operator.
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  const readsBefore = mocked.fetchExit.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchExit.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(
    await screen.findByText(/void this request and raise a fresh one/),
  ).toBeInTheDocument();
  expect(mocked.postExitSettlement).toHaveBeenCalledTimes(2);

  // The re-entered identical intent is a NEW one: the reload epoch
  // rotates the key — a backend idempotency store pinned to the
  // conflicted attempt can never serve the stale outcome.
  await attemptSettlement(user);
  await waitFor(() => expect(mocked.postExitSettlement).toHaveBeenCalledTimes(3));
  expect(mocked.postExitSettlement.mock.calls[2]?.[3]).not.toBe(key1);
  expect(await screen.findByText(/Settlement posted · ref EX-0001/)).toBeInTheDocument();
});

test("VOID: typed confirmation, triple-clicked confirm voids ONCE with the FRESH record version pinned on the wire (1.4)", async () => {
  const user = userEvent.setup();
  let release: (value: ExitRecord) => void = () => undefined;
  mocked.voidExit.mockImplementation(
    () =>
      new Promise<ExitRecord>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Void request…" }));
  const confirm = await armDanger(user, "Void request");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);

  expect(mocked.voidExit).toHaveBeenCalledTimes(1);
  expect(mocked.voidExit.mock.calls[0]?.[0]).toBe(EXIT_ID);
  // The version pins the acted-on record state — the fresh read's.
  expect(mocked.voidExit.mock.calls[0]?.[1]).toBe(7);

  release(requestedExit({ status: "rejected", version: 8 }));
  await waitFor(() => expect(mocked.voidExit).toHaveBeenCalledTimes(1));
});

test("TERMINAL statuses structurally WITHDRAW every write affordance (settled AND rejected)", async () => {
  const user = userEvent.setup();
  mocked.fetchExitsPage.mockResolvedValue({
    items: [
      approvedExit({
        status: "settled",
        settled_at: "2026-08-03T10:00:00+00:00",
        settlement_txn_id: "dddddddd-1111-2222-3333-444444444444",
      }),
    ],
    nextCursor: null,
  });
  mocked.fetchExit.mockResolvedValue(
    approvedExit({
      status: "settled",
      settled_at: "2026-08-03T10:00:00+00:00",
      settlement_txn_id: "dddddddd-1111-2222-3333-444444444444",
    }),
  );
  const view = mountScreen();

  await openDetail(user);
  expect(await screen.findByText(/terminal state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Void request…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  // The read affordance survives.
  expect(screen.getByRole("button", { name: "View exit statement" })).toBeInTheDocument();

  view.unmount();
  mocked.fetchExitsPage.mockResolvedValue({
    items: [requestedExit({ status: "rejected" })],
    nextCursor: null,
  });
  mocked.fetchExit.mockResolvedValue(requestedExit({ status: "rejected" }));
  mountScreen();
  await openDetail(user);
  expect(await screen.findByText(/terminal state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Void request…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Post settlement…" })).toBeNull();
  expect(mocked.voteOnExit).not.toHaveBeenCalled();
  expect(mocked.voidExit).not.toHaveBeenCalled();
  expect(mocked.postExitSettlement).not.toHaveBeenCalled();
});

test("query-path 401 tears the session down AND empties BOTH per-tab registries (W58-2/F-M5): the next operator inherits no maker witness and no spent votes", async () => {
  recordExitMaker(EXIT_ID, ADMIN_ID);
  recordVotedExit(EXIT_ID);
  mocked.fetchExitsPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
  expect(exitMakerOf(EXIT_ID)).toBe(EXIT_MAKER_UNKNOWN);
  expect(hasVotedOnExit(EXIT_ID)).toBe(false);
});

test("explicit sign-out empties BOTH per-tab registries (W58-2/F-M5)", async () => {
  recordExitMaker(EXIT_ID, ADMIN_ID);
  recordVotedExit(EXIT_ID);
  expect(exitMakerOf(EXIT_ID)).toBe(ADMIN_ID);
  expect(hasVotedOnExit(EXIT_ID)).toBe(true);

  // Real sign-out path: local teardown must win even if the revocation
  // call cannot complete in this harness.
  await act(async () => {
    await logout().catch(() => undefined);
  });

  expect(hasSession()).toBe(false);
  expect(exitMakerOf(EXIT_ID)).toBe(EXIT_MAKER_UNKNOWN);
  expect(hasVotedOnExit(EXIT_ID)).toBe(false);
});

test("W56-3: a stray overlay click never discards the exit-request form or the armed drawers; the read-only statement drawer keeps the light dismissal", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  // Form drawer: the half-completed entry SURVIVES the stray click…
  await user.click(await screen.findByRole("button", { name: "+ Request exit" }));
  const drawer = await screen.findByRole("dialog", { name: "Request member exit" });
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Request member exit" })).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Member")).toHaveValue(MEMBER_ID);
  // …and the explicit ✕ still closes.
  await user.click(within(drawer).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Request member exit" })).toBeNull();

  // The detail drawer bears lifecycle writes — a stray click never
  // discards it either.
  await openDetail(user);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Exit request" })).toBeInTheDocument();

  // The read-only statement drawer is NOT form-bearing: the default
  // light dismissal is the convention there (falsifiable: a blanket
  // opt-out would fail this branch).
  await user.click(screen.getByRole("button", { name: "View exit statement" }));
  await screen.findByRole("dialog", { name: "Exit statement" });
  const overlays = container.querySelectorAll('[role="presentation"]');
  await user.click(overlays[overlays.length - 1] as HTMLElement);
  expect(screen.queryByRole("dialog", { name: "Exit statement" })).toBeNull();
});
