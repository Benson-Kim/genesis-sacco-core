/**
 * Recovery worklist + case-file adversarial tests THROUGH THE REAL
 * SCREEN CODE (P15 follow-on batch 1, issue #31 — audit #30 finding
 * R1: the P13.16 collections & recovery worklist). The feature API
 * layer is stubbed at the module boundary; rendering, keyset paging,
 * permission gating, idempotency-key custody, the disposition pairing,
 * 409 handling and the typed confirmations are the real shipped code,
 * mounted under the REAL <Providers>.
 *
 * Centrepieces (the users/exits reference-harness pyramid):
 * - LEAST DISCLOSURE (addendum A5): NO money figure exists anywhere on
 *   this module — a "KES" appearing anywhere in the DOM fails these
 *   tests (falsifiable: render any balance and they go red).
 * - Dispositions offer ONLY the staff-settable targets; the job-only
 *   closes (cured / written-off) are structurally ABSENT from the UI.
 * - Target-paired rationale fields (!53 F1/F2): pause requires the
 *   reason, restructure-close requires THE outcome note — validated
 *   before any wire attempt.
 * - Attribution under the !70 short-id convention; NULL assignee is
 *   the honest "unassigned"; the unassignable flag renders honestly.
 * - EXACTLY ONE write per intent; 409s get the explicit reload flow;
 *   consecutive identical notes are DISTINCT intents (rotated keys).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { RecoveryScreen } from "../components/RecoveryScreen";
import {
  caseSchema,
  LIVE_CASE_STATUSES,
  WORKLIST_STATUS_FILTERS,
  worklistRowSchema,
  type CaseNote,
  type CaseRecord,
  type WorklistRow,
} from "../schemas";
import * as recoveryApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchWorklistPage: jest.fn(),
    fetchCase: jest.fn(),
    openCase: jest.fn(),
    assignCase: jest.fn(),
    setDisposition: jest.fn(),
    addCaseNote: jest.fn(),
    addOutcomeNote: jest.fn(),
    fetchCaseNotesPage: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(recoveryApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const OFFICER_ID = "88888888-8888-8888-8888-888888888888";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const LOAN_ID = "22222222-3333-4444-5555-666666666666";
const CASE_ID = "cccccccc-1111-2222-3333-444444444444";
const CASE_PHRASE = CASE_ID.slice(0, 8);

// Attacker-influenced free text on this module (the case notes) — must
// render as inert text (named XSS threat).
const HOSTILE_NOTE = '<img src=x onerror=window.__pwned=1> "called member"';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function openCaseRecord(overrides: Partial<CaseRecord> = {}): CaseRecord {
  return {
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
    ...overrides,
  };
}

function worklistRow(overrides: Partial<WorklistRow> = {}): WorklistRow {
  return {
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
    ...overrides,
  };
}

function note(overrides: Partial<CaseNote> = {}): CaseNote {
  return {
    id: "dddddddd-1111-2222-3333-444444444444",
    author_id: OFFICER_ID,
    note: "Called the member — promised to pay Friday.",
    created_at: "2026-08-02T10:00:00+00:00",
    is_outcome: false,
    ...overrides,
  };
}

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "loan_book", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

/** loan_book:view ONLY — no open-case (create), no case writes (edit). */
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

function mountScreen() {
  return render(
    <Providers>
      <RecoveryScreen />
    </Providers>,
  );
}

/** Open the case drawer by clicking the (only) worklist row. */
async function openDetail(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("120"));
  return await screen.findByRole("dialog", { name: "Recovery case" });
}

/** Type the byte-identical phrase and return the danger button. */
async function armDanger(user: ReturnType<typeof userEvent.setup>, confirmLabel: string) {
  await user.type(
    await screen.findByLabelText(`Type "${CASE_PHRASE}" to confirm`),
    CASE_PHRASE,
  );
  return screen.getByRole("button", { name: confirmLabel });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchWorklistPage.mockResolvedValue({ items: [worklistRow()], nextCursor: null });
  mocked.fetchCase.mockResolvedValue(openCaseRecord());
  mocked.openCase.mockResolvedValue(openCaseRecord());
  mocked.assignCase.mockResolvedValue(
    openCaseRecord({ assignee_id: ADMIN_ID, first_assigned_at: "2026-08-02T10:00:00+00:00", version: 3 }),
  );
  mocked.setDisposition.mockResolvedValue(openCaseRecord({ status: "disputed", version: 3 }));
  mocked.addCaseNote.mockResolvedValue(note());
  mocked.addOutcomeNote.mockResolvedValue(note({ is_outcome: true }));
  mocked.fetchCaseNotesPage.mockResolvedValue({ items: [], nextCursor: null });
});

afterEach(() => {
  clearSession();
});

test("LEAST DISCLOSURE (addendum A5): the worklist renders workflow facts ONLY — no money figure exists ANYWHERE in the DOM", async () => {
  const { container } = mountScreen();

  expect(await screen.findByText("120")).toBeInTheDocument();
  // Scoped to the register table: the (a).3 classification filter now
  // ALSO carries the vocabulary label as an <option> — the assertion
  // stays on the rendered ROW pill.
  expect(
    within(screen.getByRole("region", { name: "Table" })).getByText("Doubtful"),
  ).toBeInTheDocument();
  // Falsifiable: render any balance, penalty or provision figure and
  // this fails — the P13.16 contract carries none; neither may the DOM.
  expect(container.textContent).not.toContain("KES");
  expect(screen.getByText(/no balances/)).toBeInTheDocument();
});

test("attribution honesty on the worklist: NULL assignee renders '— (unassigned)'; a suspended-after-assignment officer renders the honest flag pill; staff UUIDs render as SHORT ids", async () => {
  mocked.fetchWorklistPage.mockResolvedValue({
    items: [
      worklistRow(),
      worklistRow({
        case_id: "eeeeeeee-1111-2222-3333-444444444444",
        assignee_id: OFFICER_ID,
        assignee_unassignable: true,
        first_assigned_at: "2026-08-02T10:00:00+00:00",
      }),
    ],
    nextCursor: null,
  });
  mountScreen();

  expect(await screen.findByText("— (unassigned)")).toBeInTheDocument();
  const cell = screen.getByTitle(OFFICER_ID);
  expect(cell).toHaveTextContent(OFFICER_ID.slice(0, 8));
  expect(screen.getByText("No longer assignable")).toBeInTheDocument();
});

test("keyset paging: Load more follows the server cursor VERBATIM (gate 1.3) with the default NO-FILTER view untouched (both filter values empty)", async () => {
  const user = userEvent.setup();
  mocked.fetchWorklistPage
    .mockResolvedValueOnce({ items: [worklistRow()], nextCursor: "opaque-cursor-§1" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchWorklistPage).toHaveBeenCalledTimes(2));
  // (filters, cursor): the DEFAULT view sends EMPTY filter values (the
  // api layer turns "" into an ABSENT parameter — byte-identical to
  // the as-built no-filter read) and the cursor verbatim.
  expect(mocked.fetchWorklistPage.mock.calls[0]?.[0]).toEqual({ status: "", classification: "" });
  expect(mocked.fetchWorklistPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchWorklistPage.mock.calls[1]?.[0]).toEqual({ status: "", classification: "" });
  expect(mocked.fetchWorklistPage.mock.calls[1]?.[1]).toBe("opaque-cursor-§1");
});

test("DECLARED filters only (issue #31 ledger (a).3): the status segment and classification select pass code-owned vocabulary values into the fetch — never free text, never a local re-sort", async () => {
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText("120");

  // The status segment offers exactly the two pause postures beyond
  // the default; a job-only or invented posture is structurally absent.
  const group = screen.getByRole("group", { name: "Status" });
  expect(within(group).getByRole("button", { name: "Open (default)" })).toBeInTheDocument();
  expect(within(group).queryByRole("button", { name: /cured/i })).toBeNull();

  await user.click(within(group).getByRole("button", { name: "Paused — disputed" }));
  await waitFor(() =>
    expect(mocked.fetchWorklistPage).toHaveBeenCalledWith(
      { status: "disputed", classification: "" },
      null,
    ),
  );

  // The classification select carries the STORED LoanClass vocabulary
  // verbatim as option values (bound server-side, never interpolated).
  const select = screen.getByLabelText("Classification");
  await user.selectOptions(select, "doubtful");
  await waitFor(() =>
    expect(mocked.fetchWorklistPage).toHaveBeenCalledWith(
      { status: "disputed", classification: "doubtful" },
      null,
    ),
  );

  // Back to the default view: EMPTY values — the params drop off the
  // wire entirely (asserted at the network layer), nothing is cached
  // across filter combinations (each is its own keyset walk).
  await user.click(within(group).getByRole("button", { name: "Open (default)" }));
  await user.selectOptions(select, "");
  await waitFor(() =>
    expect(mocked.fetchWorklistPage).toHaveBeenCalledWith(
      { status: "", classification: "" },
      null,
    ),
  );
});

test("the status filter vocabulary is PINNED to the LIVE statuses (the backend WorklistStatusParam Literal ↔ domain LIVE_STATUSES contract): the two can never drift", () => {
  // Falsifiable both ways: adding a closed status to the filter vocab
  // or removing a live one fails here.
  expect([...WORKLIST_STATUS_FILTERS]).toEqual([...LIVE_CASE_STATUSES]);
});

test("deny-by-default: loan_book:view ONLY gets no Initiate affordance and NO case-write affordances in the drawer — zero write calls possible", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("120");
  expect(screen.queryByRole("button", { name: "+ Initiate recovery" })).toBeNull();

  await openDetail(user);
  expect(
    await screen.findByText(/no loan book edit permission/),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Assignee user id")).toBeNull();
  expect(screen.queryByLabelText("Disposition")).toBeNull();
  expect(screen.queryByLabelText("Add note")).toBeNull();
  expect(mocked.assignCase).not.toHaveBeenCalled();
  expect(mocked.setDisposition).not.toHaveBeenCalled();
  expect(mocked.addCaseNote).not.toHaveBeenCalled();
});

test("hostile note text renders as inert TEXT in the permanent case file (named XSS threat)", async () => {
  const user = userEvent.setup();
  mocked.fetchCaseNotesPage.mockResolvedValue({
    items: [note({ note: HOSTILE_NOTE })],
    nextCursor: null,
  });
  const { container } = mountScreen();

  await openDetail(user);
  expect(await screen.findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("OPEN CASE: no timestamp/classification/DPD can even be entered — the form carries the loan id ONLY; one write per intent; the result renders the SERVER's NPL snapshot verbatim", async () => {
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Initiate recovery" }));
  const drawer = await screen.findByRole("dialog", { name: "Initiate recovery" });
  // The A7 posture, structurally: exactly ONE labelled entry exists.
  expect(within(drawer).getByLabelText("Loan id")).toBeInTheDocument();
  expect(within(drawer).queryByLabelText(/classification/i)).toBeNull();
  expect(within(drawer).queryByLabelText(/days past due/i)).toBeNull();
  expect(within(drawer).queryByLabelText(/date|opened/i)).toBeNull();

  await user.type(within(drawer).getByLabelText("Loan id"), LOAN_ID);
  await user.click(within(drawer).getByRole("button", { name: "Initiate recovery…" }));
  const confirm = screen.getByRole("button", { name: "Initiate recovery" });
  expect(confirm).toBeDisabled();
  expect(mocked.openCase).not.toHaveBeenCalled();
  await user.type(
    screen.getByLabelText(`Type "${LOAN_ID.slice(0, 8)}" to confirm`),
    LOAN_ID.slice(0, 8),
  );
  await user.click(confirm);
  await user.click(confirm);

  await waitFor(() => expect(mocked.openCase).toHaveBeenCalledTimes(1));
  expect(mocked.openCase.mock.calls[0]?.[0]).toBe(LOAN_ID);
  expect(await screen.findByText("Recovery case opened")).toBeInTheDocument();
  // The SERVER's snapshot verbatim: classification pill + DPD.
  expect(within(drawer).getByText("Doubtful")).toBeInTheDocument();
  expect(within(drawer).getByText("120")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Initiate recovery…" })).toBeNull();
});

test("OPEN CASE 409 (one live case per loan): EXACTLY ONE attempt; the explicit reload NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.openCase
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-live"))
    .mockResolvedValue(openCaseRecord());
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Initiate recovery" }));
  const drawer = await screen.findByRole("dialog", { name: "Initiate recovery" });
  await user.type(within(drawer).getByLabelText("Loan id"), LOAN_ID);
  await user.click(within(drawer).getByRole("button", { name: "Initiate recovery…" }));
  await user.type(
    screen.getByLabelText(`Type "${LOAN_ID.slice(0, 8)}" to confirm`),
    LOAN_ID.slice(0, 8),
  );
  await user.click(screen.getByRole("button", { name: "Initiate recovery" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.openCase).toHaveBeenCalledTimes(1);
  const staleKey = mocked.openCase.mock.calls[0]?.[1];

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(await screen.findByText(/may already carry a live case/)).toBeInTheDocument();
  expect(mocked.openCase).toHaveBeenCalledTimes(1);

  await user.click(within(drawer).getByRole("button", { name: "Initiate recovery…" }));
  await user.type(
    screen.getByLabelText(`Type "${LOAN_ID.slice(0, 8)}" to confirm`),
    LOAN_ID.slice(0, 8),
  );
  await user.click(screen.getByRole("button", { name: "Initiate recovery" }));
  await waitFor(() => expect(mocked.openCase).toHaveBeenCalledTimes(2));
  expect(mocked.openCase.mock.calls[1]?.[1]).not.toBe(staleKey);
});

test("ASSIGN: the write pins the FRESH record version; 'Assign to me' uses the session's own id — no directory is ever fetched", async () => {
  const user = userEvent.setup();
  mountScreen();

  await openDetail(user);
  await user.click(await screen.findByRole("button", { name: "Assign to me" }));

  await waitFor(() => expect(mocked.assignCase).toHaveBeenCalledTimes(1));
  expect(mocked.assignCase.mock.calls[0]?.[0]).toBe(CASE_ID);
  expect(mocked.assignCase.mock.calls[0]?.[1]).toBe(2);
  expect(mocked.assignCase.mock.calls[0]?.[2]).toBe(ADMIN_ID);
});

test("ASSIGN validation: a non-UUID assignee never leaves the screen", async () => {
  const user = userEvent.setup();
  mountScreen();

  await openDetail(user);
  await user.type(await screen.findByLabelText("Assignee user id"), "not-a-user");
  await user.click(screen.getByRole("button", { name: "Assign" }));
  expect(await screen.findByText(/full user UUID/)).toBeInTheDocument();
  expect(mocked.assignCase).not.toHaveBeenCalled();
});

test("DISPOSITIONS offer ONLY the staff-settable targets — the job-only closes (cured / written-off) are structurally ABSENT (FM2)", async () => {
  const user = userEvent.setup();
  mountScreen();

  await openDetail(user);
  const select = await screen.findByLabelText("Disposition");
  const options = within(select).getAllByRole("option");
  const values = options.map((option) => (option as HTMLOptionElement).value);
  expect(values).toEqual(
    expect.arrayContaining(["disputed", "irrecoverable_pending_write_off", "open", "closed_restructured"]),
  );
  expect(values).not.toContain("closed_cured");
  expect(values).not.toContain("closed_written_off");
});

test("DISPOSITION pairing (!53 F1/F2): a pause without the reason and a restructure-close without THE outcome note are refused BEFORE any wire attempt", async () => {
  const user = userEvent.setup();
  mountScreen();

  await openDetail(user);
  await user.selectOptions(await screen.findByLabelText("Disposition"), "disputed");
  await user.click(screen.getByRole("button", { name: "Record disposition…" }));
  expect(await screen.findByText(/reason is REQUIRED to pause/)).toBeInTheDocument();
  expect(mocked.setDisposition).not.toHaveBeenCalled();

  await user.selectOptions(screen.getByLabelText("Disposition"), "closed_restructured");
  await user.click(screen.getByRole("button", { name: "Record disposition…" }));
  expect(await screen.findByText(/outcome note is REQUIRED/)).toBeInTheDocument();
  expect(mocked.setDisposition).not.toHaveBeenCalled();
});

test("DISPOSITION (pause): typed confirmation; exactly ONE write pinning the FRESH version; a 409 gets the explicit reload and NEVER replays", async () => {
  const user = userEvent.setup();
  mocked.setDisposition
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-gate"))
    .mockResolvedValue(openCaseRecord({ status: "disputed", version: 3 }));
  mountScreen();

  await openDetail(user);
  const readsBefore = mocked.fetchCase.mock.calls.length;
  await user.selectOptions(await screen.findByLabelText("Disposition"), "disputed");
  await user.type(
    screen.getByLabelText("Reason (required to pause)"),
    "Member disputes the arrears figures",
  );
  await user.click(screen.getByRole("button", { name: "Record disposition…" }));
  const confirm = await armDanger(user, "Record disposition");
  await user.click(confirm);
  await user.click(confirm);

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.setDisposition).toHaveBeenCalledTimes(1);
  expect(mocked.setDisposition.mock.calls[0]?.[0]).toBe(CASE_ID);
  expect(mocked.setDisposition.mock.calls[0]?.[1]).toBe(2);
  expect(mocked.setDisposition.mock.calls[0]?.[2]).toMatchObject({
    status: "disputed",
    reason: "Member disputes the arrears figures",
  });

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() =>
    expect(mocked.fetchCase.mock.calls.length).toBeGreaterThan(readsBefore),
  );
  expect(await screen.findByText(/re-check the case status before acting again/)).toBeInTheDocument();
  expect(mocked.setDisposition).toHaveBeenCalledTimes(1);

  await user.click(screen.getByRole("button", { name: "Record disposition…" }));
  await user.click(await armDanger(user, "Record disposition"));
  await waitFor(() => expect(mocked.setDisposition).toHaveBeenCalledTimes(2));
});

test("NOTES: append-only file — one write per submit; consecutive IDENTICAL notes are DISTINCT intents with ROTATED keys (rule 4's repeat leg); no edit/delete affordance exists", async () => {
  const user = userEvent.setup();
  mocked.fetchCaseNotesPage.mockResolvedValue({ items: [note()], nextCursor: null });
  mountScreen();

  await openDetail(user);
  expect(await screen.findByText(/promised to pay Friday/)).toBeInTheDocument();
  // Append-only: no edit/delete affordance anywhere near the file.
  expect(screen.queryByRole("button", { name: /edit/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();

  await user.type(await screen.findByLabelText("Add note"), "Called, no answer");
  await user.click(screen.getByRole("button", { name: "Append note" }));
  await waitFor(() => expect(mocked.addCaseNote).toHaveBeenCalledTimes(1));
  expect(mocked.addCaseNote.mock.calls[0]?.[1]).toBe("Called, no answer");
  const firstKey = mocked.addCaseNote.mock.calls[0]?.[2];

  // The IDENTICAL follow-up note ("called, no answer" twice IS the
  // collections reality) is a NEW intent — the key rotates.
  await user.type(await screen.findByLabelText("Add note"), "Called, no answer");
  await user.click(screen.getByRole("button", { name: "Append note" }));
  await waitFor(() => expect(mocked.addCaseNote).toHaveBeenCalledTimes(2));
  expect(mocked.addCaseNote.mock.calls[1]?.[1]).toBe("Called, no answer");
  expect(mocked.addCaseNote.mock.calls[1]?.[2]).not.toBe(firstKey);
});

test("CLOSED case: assignment/disposition/notes affordances are structurally over; exactly ONE outcome note may be recorded; a case already carrying one offers NOTHING", async () => {
  const user = userEvent.setup();
  mocked.fetchCase.mockResolvedValue(
    openCaseRecord({
      status: "closed_cured",
      closed_at: "2026-08-03T10:00:00+00:00",
      version: 5,
    }),
  );
  const view = mountScreen();

  await openDetail(user);
  expect(await screen.findByText(/This case is closed/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Assignee user id")).toBeNull();
  expect(screen.queryByLabelText("Disposition")).toBeNull();
  expect(screen.queryByLabelText("Add note")).toBeNull();

  // THE one outcome note is offered…
  await user.type(
    await screen.findByLabelText("Closing outcome note (one per case)"),
    "Cured after the June salary round.",
  );
  await user.click(screen.getByRole("button", { name: "Record outcome note" }));
  await waitFor(() => expect(mocked.addOutcomeNote).toHaveBeenCalledTimes(1));
  expect(mocked.addOutcomeNote.mock.calls[0]?.[1]).toBe("Cured after the June salary round.");

  // …and once the file carries it, the affordance is GONE.
  view.unmount();
  mocked.fetchCaseNotesPage.mockResolvedValue({
    items: [note({ is_outcome: true, note: "Cured after the June salary round." })],
    nextCursor: null,
  });
  mountScreen();
  await openDetail(user);
  expect(await screen.findByText("Outcome note")).toBeInTheDocument();
  expect(screen.queryByLabelText("Closing outcome note (one per case)")).toBeNull();
  expect(mocked.addOutcomeNote).toHaveBeenCalledTimes(1);
});

test("attribution in the drawer (the !70 pattern): opened_by renders as the bare-UUID SHORT id with the full UUID on title — never a name or email", async () => {
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openDetail(user);
  await within(drawer).findByText("Record version");
  const cell = within(drawer).getAllByTitle(OFFICER_ID)[0];
  expect(cell).toHaveTextContent(OFFICER_ID.slice(0, 8));
  expect(within(drawer).queryByText(/@/)).toBeNull();
});

test("boundary REJECTS hostile shapes (real Zod, unmocked): unknown case status, garbage timestamp, non-NPL open classification can never arm a write", () => {
  expect(
    caseSchema.safeParse({ ...openCaseRecord(), status: "escalated" }).success,
  ).toBe(false);
  expect(
    caseSchema.safeParse({ ...openCaseRecord(), opened_at: "yesterday-ish" }).success,
  ).toBe(false);
  expect(
    caseSchema.safeParse({ ...openCaseRecord(), classification_at_open: "normal" }).success,
  ).toBe(false);
  // The WORKLIST accepts the full LoanClass set (a paying loan drops
  // bands before the nightly close pass cures the case)…
  expect(
    worklistRowSchema.safeParse({ ...worklistRow(), classification: "normal" }).success,
  ).toBe(true);
  // …but never unknown vocabulary.
  expect(
    worklistRowSchema.safeParse({ ...worklistRow(), classification: "zombie" }).success,
  ).toBe(false);
});

test("query-path 401 tears the session down (the P13.16 worklist is not exempt from the teardown)", async () => {
  mocked.fetchWorklistPage.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("W56-3: a stray overlay click never discards the open-case entry or the case drawer's in-progress work", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  await user.click(await screen.findByRole("button", { name: "+ Initiate recovery" }));
  const drawer = await screen.findByRole("dialog", { name: "Initiate recovery" });
  await user.type(within(drawer).getByLabelText("Loan id"), LOAN_ID);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Initiate recovery" })).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Loan id")).toHaveValue(LOAN_ID);
  await user.click(within(drawer).getByRole("button", { name: "Close" }));

  await openDetail(user);
  await user.type(await screen.findByLabelText("Add note"), "half-typed note");
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Recovery case" })).toBeInTheDocument();
  expect(screen.getByLabelText("Add note")).toHaveValue("half-typed note");
});
