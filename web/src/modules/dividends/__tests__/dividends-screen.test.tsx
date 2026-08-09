/**
 * Dividends register + lifecycle-write adversarial tests THROUGH THE
 * REAL SCREEN CODE (issue #31 batch 2 — a tenant-wide money surface:
 * the distribution run pays every eligible member). The feature API
 * layer is stubbed at the module boundary; rendering, keyset paging,
 * permission gating, idempotency-key custody, the fresh reads, SoD
 * structural withholding, 409 handling and the typed confirmations are
 * the real shipped code, mounted under the REAL <Providers>.
 *
 * Centrepieces:
 * - EVERY write (declare, vote, void, distribute) has a triple-click
 *   single-write proof and a typed ConfirmDangerModal (W58-3).
 * - The DECLARE drawer proves the v1.1 rule-1 posture structurally: NO
 *   input field exists in it at all — no rate, period or figure can
 *   even be typed.
 * - F-M1: the distribute mutate path STRUCTURALLY refuses to fire
 *   without the loaded fresh record.
 * - SoD (blocker (f)): the witnessed declarer gets NO vote/run
 *   controls (structurally withheld); the UNKNOWN maker renders
 *   controls with the HONEST contract-gap label (DeclarationOut
 *   exposes no attribution field — recorded on #31); the server 403
 *   renders the least-disclosure banner.
 * - Votes are SPENT across remounts (votedRegistry, W58-6) and BOTH
 *   per-tab registries die on the query-path 401 AND explicit
 *   sign-out (registerSessionScopedStore — W58-2).
 * - A PARTIAL distribution run (pending_members > 0) keeps the re-run
 *   affordance armed and the re-run is a NEW intent (rotated key);
 *   a COMPLETED run spends the affordance.
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { logout } from "@/modules/auth/api";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { DividendsScreen } from "../components/DividendsScreen";
import { DistributeDialog } from "../components/DistributeDialog";
import {
  clearDividendMakers,
  DIVIDEND_MAKER_UNKNOWN,
  dividendMakerOf,
  recordDividendMaker,
} from "../makerRegistry";
import {
  clearVotedDeclarations,
  hasVotedOnDeclaration,
  recordVotedDeclaration,
} from "../votedRegistry";
import {
  declarationSchema,
  type DeclarationRecord,
  type DistributionRun,
  type DividendVoteResult,
} from "../schemas";
import * as dividendsApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchDeclarationsPage: jest.fn(),
    fetchDeclaration: jest.fn(),
    declareDividend: jest.fn(),
    voteOnDeclaration: jest.fn(),
    voidDeclaration: jest.fn(),
    distributeDeclaration: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(dividendsApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const OTHER_OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const DECL_ID = "dddddddd-aaaa-bbbb-cccc-111111111111";

/** ConfirmDangerModal phrase for the record-scoped writes. */
const PHRASE = DECL_ID.slice(0, 8);

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(claims: object): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ ...claims, exp })}.sig`;
}

function declaredDeclaration(overrides: Partial<DeclarationRecord> = {}): DeclarationRecord {
  return {
    id: DECL_ID,
    fy_start: "2025-07-01",
    fy_end: "2026-06-30",
    dividend_rate_pct: "14.00",
    rebate_rate_pct: "9.00",
    eligible_members: 42,
    total_share_basis: "500000.00",
    total_deposit_basis: "1200000.10",
    total_dividend: "70000.10",
    total_rebate: "108000.00",
    total_payout: "178000.10",
    status: "declared",
    // Fixture default: an UNATTRIBUTED server record (requested_by
    // null — e.g. a system-actor declaration). The witness-fallback
    // legs below exercise exactly this branch; the server-truth legs
    // override it per test (issue #31 ledger (a).4).
    requested_by: null,
    decided_at: null,
    distributed_at: null,
    version: 7,
    created_at: "2026-08-01T09:00:00+00:00",
    ...overrides,
  };
}

function approvedDeclaration(overrides: Partial<DeclarationRecord> = {}): DeclarationRecord {
  return declaredDeclaration({
    status: "approved",
    decided_at: "2026-08-02T10:00:00+00:00",
    ...overrides,
  });
}

const TALLY_OPEN: DividendVoteResult = {
  approvals: 1,
  rejections: 0,
  decision: null,
  status: "declared",
};

/** A PARTIAL run: one member concurrency-skipped, one mid-run exit
 * disposed as unclaimed (issue #19) — status stays approved. */
const PARTIAL_RUN: DistributionRun = {
  scanned: 42,
  claimed: 40,
  skipped_zero: 0,
  skipped_claimed: 0,
  unclaimed: 1,
  dividend_total: "68000.10",
  rebate_total: "106000.00",
  payout_total: "174000.10",
  pending_members: 1,
  batches: 1,
  status: "approved",
};

/** The reconciling re-run: everything claimed, status distributed. */
const COMPLETE_RUN: DistributionRun = {
  scanned: 1,
  claimed: 1,
  skipped_zero: 0,
  skipped_claimed: 41,
  unclaimed: 0,
  dividend_total: "2000.00",
  rebate_total: "2000.00",
  payout_total: "4000.00",
  pending_members: 0,
  batches: 1,
  status: "distributed",
};

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "transactions",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

/** transactions:view ONLY — no edit (no declare), no approve (no
 * vote/void/distribute). */
const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    {
      module: "transactions",
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
      <DividendsScreen />
    </Providers>,
  );
}

/** Open the detail drawer by clicking the (only) register row. */
async function openDetail(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("KES 178,000.10"));
  return await screen.findByRole("dialog", { name: "Dividend declaration" });
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

/** Walk detail drawer → distribute dialog for an APPROVED record. */
async function openDistributeDialog(user: ReturnType<typeof userEvent.setup>) {
  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Run distribution…" }));
  await screen.findByText("Pinned record version");
}

/** Arm and fire one distribution attempt. */
async function attemptRun(
  user: ReturnType<typeof userEvent.setup>,
  buttonLabel = "Run distribution…",
) {
  await user.click(screen.getByRole("button", { name: buttonLabel }));
  await user.click(await armDanger(user, "Run distribution"));
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt({ sub: ADMIN_ID }), refreshToken: "refresh-1" });
  clearDividendMakers();
  clearVotedDeclarations();
  grantPermissions(FULL_PERMS);
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [declaredDeclaration()],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(declaredDeclaration());
  mocked.declareDividend.mockResolvedValue(declaredDeclaration());
  mocked.voteOnDeclaration.mockResolvedValue(TALLY_OPEN);
  mocked.voidDeclaration.mockResolvedValue(declaredDeclaration({ status: "rejected", version: 8 }));
  mocked.distributeDeclaration.mockResolvedValue(PARTIAL_RUN);
});

afterEach(() => {
  clearSession();
});

test("register renders every declaration figure VERBATIM in its own labelled column — trailing cents survive, rates render as rates (never fmtKes), no totals row exists (blocker (a))", async () => {
  mountScreen();

  // Trailing cents survive — a numeric round-trip would drop them.
  expect(await screen.findByText("KES 178,000.10")).toBeInTheDocument();
  expect(screen.getByText("KES 70,000.10")).toBeInTheDocument();
  expect(screen.getByText("KES 108,000.00")).toBeInTheDocument();
  // Rates are RATES: rendered with their two-place column value as a
  // percentage, never through the money formatter.
  expect(screen.getByText("14.00% / 9.00%")).toBeInTheDocument();
  expect(screen.queryByText("KES 14.00")).toBeNull();
  // The FY bounds are calendar dates, verbatim.
  expect(screen.getByText(/2025-07-01 → 2026-06-30/)).toBeInTheDocument();
  // No client-side arithmetic anywhere: payout = dividend + rebate is
  // the DB's CHECK — no recomputed figure, no totals row.
  expect(screen.queryByText(/Totals/)).toBeNull();
  expect(screen.getByText(/payout = dividend \+ rebate is the/)).toBeInTheDocument();
});

test("keyset paging: Load more follows the server cursor VERBATIM (gate 1.3); no filter affordance exists (the contract declares none)", async () => {
  const user = userEvent.setup();
  mocked.fetchDeclarationsPage
    .mockResolvedValueOnce({ items: [declaredDeclaration()], nextCursor: "opaque-cursor-§1" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchDeclarationsPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchDeclarationsPage.mock.calls[0]?.[0]).toBeNull();
  expect(mocked.fetchDeclarationsPage.mock.calls[1]?.[0]).toBe("opaque-cursor-§1");

  // No filter controls: the P13.11 list contract declares none — a
  // client-side filter would be local filtering (gate 1.3 violation).
  expect(screen.queryByLabelText("Status")).toBeNull();
});

test("deny-by-default: a view-only role gets NO declare affordance; vote/void/distribute affordances are withheld in the drawers; zero writes possible", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("KES 178,000.10");
  expect(screen.queryByRole("button", { name: "+ Declare dividend" })).toBeNull();

  await openDetail(user);
  expect(await screen.findByText(/no transactions approval permission/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Void declaration…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run distribution…" })).toBeNull();
  expect(mocked.declareDividend).not.toHaveBeenCalled();
  expect(mocked.voteOnDeclaration).not.toHaveBeenCalled();
  expect(mocked.voidDeclaration).not.toHaveBeenCalled();
  expect(mocked.distributeDeclaration).not.toHaveBeenCalled();
});

test("hostile record id renders as inert TEXT via the short-id convention (named XSS threat) — no markup materialises", async () => {
  const HOSTILE_ID = "<img src=x onerror=window.__pwned=1>";
  const user = userEvent.setup();
  mocked.fetchDeclaration.mockResolvedValue(declaredDeclaration({ id: HOSTILE_ID }));
  const { container } = mountScreen();

  await openDetail(user);
  await screen.findByText("Record version");
  // The sliced 8-char prefix ("<img src") renders as literal text…
  expect(screen.getByTitle(HOSTILE_ID)).toHaveTextContent("<img src");
  // …and never as an element.
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("hostile money/status shapes are REJECTED by the real boundary and can never render — the register consumes the rejection as an error state", async () => {
  // The REAL (unmocked) Zod boundary rejects hostile money, unknown
  // status vocabulary and markup-bearing strings…
  expect(
    declarationSchema.safeParse({ ...declaredDeclaration(), total_payout: "<b>178000.10</b>" })
      .success,
  ).toBe(false);
  expect(
    declarationSchema.safeParse({ ...declaredDeclaration(), total_rebate: "1e5" }).success,
  ).toBe(false);
  expect(
    declarationSchema.safeParse({ ...declaredDeclaration(), status: "voided" }).success,
  ).toBe(false);
  // …so such a page can only reach the screen as a rejected fetch: the
  // register renders the error state, never the garbage.
  mocked.fetchDeclarationsPage.mockRejectedValue(new Error("contract violation at the boundary"));
  mountScreen();
  expect(
    await screen.findByText(/Could not load this list/, undefined, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/178,000/)).toBeNull();
});

test("DECLARE (v1.1 rule 1, STRUCTURAL): the drawer contains NO input field at all — no rate, period or figure can even be typed; a triple-clicked confirmation produces exactly ONE write; the result panel renders the snapshot verbatim; the maker is witnessed (SoD)", async () => {
  const user = userEvent.setup();
  let releaseDeclare: (value: DeclarationRecord) => void = () => undefined;
  mocked.declareDividend.mockImplementation(
    () =>
      new Promise<DeclarationRecord>((resolve) => {
        releaseDeclare = resolve;
      }),
  );
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Declare dividend" }));
  const drawer = await screen.findByRole("dialog", { name: "Declare dividend" });

  // STRUCTURAL rule-1 proof: no textbox, spinbutton, combobox or any
  // other input exists in the drawer — the server owns every figure.
  expect(within(drawer).queryAllByRole("textbox")).toHaveLength(0);
  expect(within(drawer).queryAllByRole("spinbutton")).toHaveLength(0);
  expect(within(drawer).queryAllByRole("combobox")).toHaveLength(0);

  await user.click(within(drawer).getByRole("button", { name: "Declare dividend…" }));

  // Typed phrase (the fixed intent word — no record id exists yet),
  // then a triple click — the pending short-circuit yields ONE write.
  const confirm = await armDanger(user, "Declare dividend", "declare");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.declareDividend).toHaveBeenCalledTimes(1);

  releaseDeclare(declaredDeclaration());

  // The result panel replaces the action (SPENT affordance) with the
  // SERVER's snapshot verbatim…
  expect(
    await screen.findByText(/Dividend declared · awaiting committee decision/),
  ).toBeInTheDocument();
  expect(within(drawer).getAllByText("KES 178,000.10").length).toBeGreaterThan(0);
  expect(within(drawer).getByText("14.00% p.a.")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Declare dividend…" })).toBeNull();
  expect(mocked.declareDividend).toHaveBeenCalledTimes(1);
  // …and this tab witnessed the maker for the SoD panels.
  expect(dividendMakerOf(DECL_ID)).toBe(ADMIN_ID);
});

test("DECLARE 409 (FY slot / unconfigured / zero payout): EXACTLY ONE attempt, the explicit reload NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.declareDividend
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-fy"))
    .mockResolvedValue(declaredDeclaration());
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Declare dividend" }));
  const drawer = await screen.findByRole("dialog", { name: "Declare dividend" });
  await user.click(within(drawer).getByRole("button", { name: "Declare dividend…" }));
  await user.click(await armDanger(user, "Declare dividend", "declare"));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(screen.getByText(/live declaration already exists/)).toBeInTheDocument();
  expect(mocked.declareDividend).toHaveBeenCalledTimes(1);
  const staleKey = mocked.declareDividend.mock.calls[0]?.[0];

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  // Unique to the notice banner (the announcer's live region carries a
  // different phrasing — asserting shared words would double-match).
  expect(
    await screen.findByText(/Check the existing declarations and the tenant/),
  ).toBeInTheDocument();
  expect(mocked.declareDividend).toHaveBeenCalledTimes(1);

  // Re-entering the IDENTICAL intent is a NEW one — rotated key.
  await user.click(within(drawer).getByRole("button", { name: "Declare dividend…" }));
  await user.click(await armDanger(user, "Declare dividend", "declare"));
  await waitFor(() => expect(mocked.declareDividend).toHaveBeenCalledTimes(2));
  expect(mocked.declareDividend.mock.calls[1]?.[0]).not.toBe(staleKey);
});

test("VOTE (W58-3): typed confirmation starts DISARMED; a triple-clicked confirm records exactly ONE vote; the tally renders SERVER counts; the affordance is spent", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  let release: (value: DividendVoteResult) => void = () => undefined;
  mocked.voteOnDeclaration.mockImplementation(
    () =>
      new Promise<DividendVoteResult>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Vote approve" }));
  // Disabled until the byte-identical phrase — a reflex click writes nothing.
  const disarmed = screen.getByRole("button", { name: "Confirm approve vote" });
  expect(disarmed).toBeDisabled();
  expect(mocked.voteOnDeclaration).not.toHaveBeenCalled();

  const confirm = await armDanger(user, "Confirm approve vote");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
  expect(mocked.voteOnDeclaration.mock.calls[0]?.[0]).toBe(DECL_ID);
  expect(mocked.voteOnDeclaration.mock.calls[0]?.[1]).toBe("approve");

  release(TALLY_OPEN);
  expect(await screen.findByText("1 approve")).toBeInTheDocument();
  expect(screen.getByText("0 reject")).toBeInTheDocument();
  expect(screen.getByText("Awaiting quorum")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeDisabled();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
});

test("VOTE SPENT ACROSS REMOUNTS (W58-6): closing and reopening the drawer never re-arms the vote buttons in this tab", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  const view = mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Vote approve" }));
  await user.click(await armDanger(user, "Confirm approve vote"));
  expect(await screen.findByText("1 approve")).toBeInTheDocument();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
  expect(hasVotedOnDeclaration(DECL_ID)).toBe(true);

  // Full remount: component state (the tally panel) is gone — the
  // per-tab registry keeps the affordance withdrawn regardless.
  view.unmount();
  mountScreen();
  await openDetail(user);
  expect(await screen.findByText(/This tab already recorded your vote/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeDisabled();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
});

test("VOTE 409 (already voted / status moved): EXACTLY ONE attempt; the explicit reload refetches the record and NEVER replays the vote", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  mocked.voteOnDeclaration.mockRejectedValue(new ApiError(409, "conflict", "corr-voted"));
  mountScreen();

  await openDetail(user);
  const readsBefore = mocked.fetchDeclaration.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Vote reject" }));
  await user.click(await armDanger(user, "Confirm reject vote"));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchDeclaration.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(
    await screen.findByText(/Record reloaded — re-check the declaration status/),
  ).toBeInTheDocument();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
});

test("SoD STRUCTURAL (blocker (f), vote): the witnessed declarer gets NO vote affordances — the panel never mounts checker controls for the maker", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, ADMIN_ID);
  mountScreen();

  await openDetail(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  expect(screen.getByText(/You declared this dividend/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(mocked.voteOnDeclaration).not.toHaveBeenCalled();
});

test("attribution honesty (NULL leg): an UNATTRIBUTED record (requested_by null) with no tab witness renders the vote controls with the HONEST label — nothing is invented; the server 403 renders the least-disclosure banner, session intact", async () => {
  const user = userEvent.setup();
  // NO recordDividendMaker call AND requested_by is null (the fixture
  // default): both the server record and this tab are honest about
  // not knowing the declarer — the label states it plainly.
  mocked.voteOnDeclaration.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  expect(dividendMakerOf(DECL_ID)).toBe(DIVIDEND_MAKER_UNKNOWN);
  await openDetail(user);
  expect(
    await screen.findByText(/unattributed on the server record .*attribution is never invented/),
  ).toBeInTheDocument();
  // The NULL affordance renders in BOTH consumption sites: the
  // register's "Declared by" column and the drawer's detail grid.
  expect(screen.getAllByText("— (unattributed)").length).toBeGreaterThanOrEqual(2);
  // Controls DO render (the server remains the enforcer)…
  await user.click(screen.getByRole("button", { name: "Vote approve" }));
  await user.click(await armDanger(user, "Confirm approve vote"));

  // …and the server's 403 renders sanitized: category text + ref only.
  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(mocked.voteOnDeclaration).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("SERVER TRUTH SUPERSEDES the per-tab witness (issue #31 ledger (a).4): a record attributed to YOU withholds the vote controls even though this tab witnessed nothing; the short id renders under least disclosure", async () => {
  const user = userEvent.setup();
  // NO recordDividendMaker call — the registry knows nothing; the
  // SERVER attributes the declaration to the signed-in operator.
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [declaredDeclaration({ requested_by: ADMIN_ID })],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(declaredDeclaration({ requested_by: ADMIN_ID }));
  mountScreen();

  expect(dividendMakerOf(DECL_ID)).toBe(DIVIDEND_MAKER_UNKNOWN);
  await openDetail(user);
  // The panel's SoD self-check matches on SERVER truth — checker
  // controls never mount for the attributed declarer.
  expect(
    await screen.findByText(/You declared this dividend \(server attribution\)/),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(mocked.voteOnDeclaration).not.toHaveBeenCalled();
});

test("SERVER TRUTH SUPERSEDES a CONTRADICTING witness: the tab witnessed the signed-in operator as maker, but the server attributes ANOTHER officer — controls render (the witness is demoted, never preferred) and the short id renders with the full UUID as title", async () => {
  const user = userEvent.setup();
  // The per-tab registry claims the signed-in operator declared it…
  recordDividendMaker(DECL_ID, ADMIN_ID);
  // …but the SERVER record attributes a different officer: server
  // truth wins and the checker controls mount.
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [declaredDeclaration({ requested_by: OTHER_OFFICER_ID })],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(
    declaredDeclaration({ requested_by: OTHER_OFFICER_ID }),
  );
  mountScreen();

  await openDetail(user);
  expect(
    await screen.findByText(
      new RegExp(`Declaring officer ${OTHER_OFFICER_ID.slice(0, 8)} \\(server attribution\\)`),
    ),
  ).toBeInTheDocument();
  // Short-id render, full UUID only in the title attribute (least
  // disclosure — no name/email is ever fetched). The drawer's Kv and
  // the register column both carry it.
  const attributed = screen.getAllByTitle(OTHER_OFFICER_ID);
  expect(attributed.length).toBeGreaterThan(0);
  for (const el of attributed) {
    expect(el).toHaveTextContent(OTHER_OFFICER_ID.slice(0, 8));
  }
  expect(screen.getByRole("button", { name: "Vote approve" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Vote reject" })).toBeEnabled();
});

test("attribution XSS inertness: a hostile requested_by string renders as inert sliced TEXT (no element injection anywhere)", async () => {
  const user = userEvent.setup();
  const hostile = "<img src=x onerror=window.__pwned=1>";
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [declaredDeclaration({ requested_by: hostile })],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(declaredDeclaration({ requested_by: hostile }));
  const { container } = mountScreen();

  // The register column renders the sliced hostile string as TEXT.
  expect(await screen.findByText(hostile.slice(0, 8))).toBeInTheDocument();
  await openDetail(user);
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("DISTRIBUTE (F-M1 + single write): the dialog FRESH-READS the record (staleTime 0 — a second read on top of the drawer's); a triple-clicked confirm runs ONCE; the run report renders VERBATIM incl. the issue-#19 unclaimed leg; a PARTIAL run keeps the re-run armed", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [approvedDeclaration()],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(approvedDeclaration());
  let release: (value: DistributionRun) => void = () => undefined;
  mocked.distributeDeclaration.mockImplementation(
    () =>
      new Promise<DistributionRun>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  const readsAfterDrawer = mocked.fetchDeclaration.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Run distribution…" }));
  await screen.findByText("Pinned record version");
  // STALE-READ DOUBLE-RUN PREVENTION: the dialog re-fetches the record
  // instead of serving the drawer's cache (record class, staleTime 0).
  await waitFor(() =>
    expect(mocked.fetchDeclaration.mock.calls.length).toBeGreaterThan(readsAfterDrawer),
  );

  await user.click(screen.getByRole("button", { name: "Run distribution…" }));
  const confirm = await armDanger(user, "Run distribution");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(1);
  expect(mocked.distributeDeclaration.mock.calls[0]?.[0]).toBe(DECL_ID);

  release(PARTIAL_RUN);

  // The run report renders the SERVER's figures verbatim — the
  // unclaimed disposition (a mid-run exit, issue #19) is DISCLOSED,
  // never dropped…
  expect(
    await screen.findByText(/Distribution run report · members still pending/),
  ).toBeInTheDocument();
  expect(screen.getByText("KES 174,000.10")).toBeInTheDocument();
  expect(screen.getByText(/parked as an unclaimed-dividends payable/)).toBeInTheDocument();
  expect(screen.getByText(/re-run the distribution to pick them up/)).toBeInTheDocument();
  // …and the affordance stays ARMED for the idempotent re-run (the
  // declaration is still approved — pending members remain).
  expect(screen.getByRole("button", { name: "Re-run distribution…" })).toBeInTheDocument();
});

test("DISTRIBUTE re-run is a NEW intent (README MATERIAL rule 4): the partial-run re-run carries a ROTATED key; a COMPLETED run spends the affordance", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [approvedDeclaration()],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(approvedDeclaration());
  mocked.distributeDeclaration
    .mockResolvedValueOnce(PARTIAL_RUN)
    .mockResolvedValueOnce(COMPLETE_RUN);
  mountScreen();

  await openDistributeDialog(user);
  await attemptRun(user);
  await screen.findByText(/members still pending/);
  const firstKey = mocked.distributeDeclaration.mock.calls[0]?.[1];

  // The re-run: byte-identical wire body, but a NEW logical intent —
  // the per-success counter rotates the key so the server can never
  // serve the stored first-run report instead of scanning again.
  await attemptRun(user, "Re-run distribution…");
  await waitFor(() => expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(2));
  expect(mocked.distributeDeclaration.mock.calls[1]?.[1]).not.toBe(firstKey);

  // The reconciling run completes the declaration: the affordance is
  // SPENT (the fresh record refetch will show status distributed; the
  // report panel already reports it).
  expect(await screen.findByText(/Distribution completed · declaration reconciled/)).toBeInTheDocument();
});

test("DISTRIBUTE key custody: STABLE across a pure 5xx retry of one intent; a 409 gets ONE attempt, the explicit reload never replays and ROTATES the key (!60 F3); the drift remedy is stated", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, OTHER_OFFICER_ID);
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [approvedDeclaration()],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(approvedDeclaration());
  mocked.distributeDeclaration
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-drift"))
    .mockResolvedValue(COMPLETE_RUN);
  mountScreen();

  await openDistributeDialog(user);

  // Attempt 1: 5xx. Attempt 2 (identical intent): the key is STABLE.
  await attemptRun(user);
  await waitFor(() => expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(1));
  await attemptRun(user);
  await waitFor(() => expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(2));
  const key1 = mocked.distributeDeclaration.mock.calls[0]?.[1];
  expect(mocked.distributeDeclaration.mock.calls[1]?.[1]).toBe(key1);

  // Attempt 2 hit the DRIFT 409: one attempt, conflict banner, remedy.
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  const readsBefore = mocked.fetchDeclaration.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchDeclaration.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(
    await screen.findByText(/void this declaration and declare afresh/),
  ).toBeInTheDocument();
  expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(2);

  // The re-entered identical intent is a NEW one: the reload epoch
  // rotates the key — a backend idempotency store pinned to the
  // conflicted attempt can never serve the stale outcome.
  await attemptRun(user);
  await waitFor(() => expect(mocked.distributeDeclaration).toHaveBeenCalledTimes(3));
  expect(mocked.distributeDeclaration.mock.calls[2]?.[1]).not.toBe(key1);
});

test("DISTRIBUTE SoD STRUCTURAL (blocker (f)): the witnessed declarer gets NO run controls in the distribution dialog — zero writes possible", async () => {
  const user = userEvent.setup();
  recordDividendMaker(DECL_ID, ADMIN_ID);
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [approvedDeclaration()],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(approvedDeclaration());
  mountScreen();

  await openDistributeDialog(user);
  expect(
    await screen.findByText(/Approval requires a DIFFERENT\s+administrator/),
  ).toBeInTheDocument();
  // Structurally withheld: no run button — not merely disabled
  // (MakerCheckerPanel has no override prop).
  expect(screen.queryByRole("button", { name: "Run distribution…" })).toBeNull();
  expect(mocked.distributeDeclaration).not.toHaveBeenCalled();
});

test("DISTRIBUTE F-M1 STRUCTURAL: while the fresh read has NOT landed, no run affordance exists at all — zero writes possible from this dialog", async () => {
  // Mount the dialog directly with a never-resolving fresh read: the
  // loading state carries NO panel and NO confirm.
  mocked.fetchDeclaration.mockImplementation(() => new Promise<DeclarationRecord>(() => undefined));
  render(
    <Providers>
      <DistributeDialog declarationId={DECL_ID} onClose={() => undefined} />
    </Providers>,
  );

  expect(await screen.findByText(/Loading declaration…/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Run distribution…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run distribution" })).toBeNull();
  expect(mocked.distributeDeclaration).not.toHaveBeenCalled();
});

test("VOID: typed confirmation, triple-clicked confirm voids ONCE with the FRESH record version pinned on the wire (1.4)", async () => {
  const user = userEvent.setup();
  let release: (value: DeclarationRecord) => void = () => undefined;
  mocked.voidDeclaration.mockImplementation(
    () =>
      new Promise<DeclarationRecord>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await openDetail(user);
  await user.click(screen.getByRole("button", { name: "Void declaration…" }));
  const confirm = await armDanger(user, "Void declaration");
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);

  expect(mocked.voidDeclaration).toHaveBeenCalledTimes(1);
  expect(mocked.voidDeclaration.mock.calls[0]?.[0]).toBe(DECL_ID);
  // The version pins the acted-on record state — the fresh read's.
  expect(mocked.voidDeclaration.mock.calls[0]?.[1]).toBe(7);

  release(declaredDeclaration({ status: "rejected", version: 8 }));
  await waitFor(() => expect(mocked.voidDeclaration).toHaveBeenCalledTimes(1));
});

test("TERMINAL statuses structurally WITHDRAW every write affordance (rejected AND distributed)", async () => {
  const user = userEvent.setup();
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [declaredDeclaration({ status: "rejected" })],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(declaredDeclaration({ status: "rejected" }));
  const view = mountScreen();

  await openDetail(user);
  expect(await screen.findByText(/terminal state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Vote approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Vote reject" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Void declaration…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run distribution…" })).toBeNull();

  view.unmount();
  mocked.fetchDeclarationsPage.mockResolvedValue({
    items: [
      approvedDeclaration({ status: "distributed", distributed_at: "2026-08-03T10:00:00+00:00" }),
    ],
    nextCursor: null,
  });
  mocked.fetchDeclaration.mockResolvedValue(
    approvedDeclaration({ status: "distributed", distributed_at: "2026-08-03T10:00:00+00:00" }),
  );
  mountScreen();
  await openDetail(user);
  expect(await screen.findByText(/terminal state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Void declaration…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run distribution…" })).toBeNull();
  expect(mocked.voteOnDeclaration).not.toHaveBeenCalled();
  expect(mocked.voidDeclaration).not.toHaveBeenCalled();
  expect(mocked.distributeDeclaration).not.toHaveBeenCalled();
});

test("query-path 401 tears the session down AND empties BOTH per-tab registries (W58-2): the next operator inherits no maker witness and no spent votes", async () => {
  recordDividendMaker(DECL_ID, ADMIN_ID);
  recordVotedDeclaration(DECL_ID);
  mocked.fetchDeclarationsPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
  expect(dividendMakerOf(DECL_ID)).toBe(DIVIDEND_MAKER_UNKNOWN);
  expect(hasVotedOnDeclaration(DECL_ID)).toBe(false);
});

test("explicit sign-out empties BOTH per-tab registries (W58-2)", async () => {
  recordDividendMaker(DECL_ID, ADMIN_ID);
  recordVotedDeclaration(DECL_ID);
  expect(dividendMakerOf(DECL_ID)).toBe(ADMIN_ID);
  expect(hasVotedOnDeclaration(DECL_ID)).toBe(true);

  // Real sign-out path: local teardown must win even if the revocation
  // call cannot complete in this harness.
  await act(async () => {
    await logout().catch(() => undefined);
  });

  expect(hasSession()).toBe(false);
  expect(dividendMakerOf(DECL_ID)).toBe(DIVIDEND_MAKER_UNKNOWN);
  expect(hasVotedOnDeclaration(DECL_ID)).toBe(false);
});

test("W56-3: a stray overlay click never discards the armed drawers (declare, detail, distribute) — every one is form-bearing/money-adjacent and opts out of overlay dismissal", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  // The declare drawer survives the stray click…
  await user.click(await screen.findByRole("button", { name: "+ Declare dividend" }));
  const drawer = await screen.findByRole("dialog", { name: "Declare dividend" });
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Declare dividend" })).toBeInTheDocument();
  // …and the explicit Cancel still closes.
  await user.click(within(drawer).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("dialog", { name: "Declare dividend" })).toBeNull();

  // The detail drawer bears lifecycle writes — a stray click never
  // discards it either.
  await openDetail(user);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Dividend declaration" })).toBeInTheDocument();
});
