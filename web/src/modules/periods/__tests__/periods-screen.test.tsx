/**
 * Accounting-periods register + close-write adversarial tests THROUGH
 * THE REAL SCREEN CODE (issue #31 batch 4 — ledger governance: the
 * close freezes a month for posting). The feature API layer is stubbed
 * at the module boundary; rendering, keyset paging, permission gating,
 * idempotency-key custody, 409 handling and the typed confirmation are
 * the real shipped code, mounted under the REAL <Providers>.
 *
 * Centrepieces:
 * - The close has a triple-click single-write proof and a typed
 *   ConfirmDangerModal (W58-3) whose phrase is the month being frozen.
 * - The dialog's ONLY inputs are the calendar year and month — the
 *   exact ClosePeriodBody contract; nothing else can even be typed
 *   (v1.1 rule 1 posture: bounds/elapsed rules are server-resolved).
 * - 409 → EXACTLY ONE attempt + the shared ConflictBanner; the
 *   explicit reload NEVER replays and the re-entered intent carries a
 *   ROTATED Idempotency-Key (!60 F3).
 * - Deny-by-default: a view-only role gets NO close affordance.
 * - The A5 PeriodContextNote SELF-GATES on transactions:view, renders
 *   the latest CLOSED month verbatim, states the no-closes case
 *   honestly and degrades to an explicit "unavailable" line on a read
 *   failure (never implying "everything open").
 * - NO per-tab registry exists in this module (no maker-checker or
 *   committee contract on P12.5), so there is no teardown surface —
 *   and no fresh copy of the S3 primitive (createSessionScopedRegistry
 *   remains consumed only where a registry exists).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { PeriodsScreen } from "../components/PeriodsScreen";
import { PeriodContextNote } from "../components/PeriodContextNote";
import { periodSchema, type PeriodRecord } from "../schemas";
import * as periodsApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchPeriodsPage: jest.fn(),
    closePeriod: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(periodsApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";

/** ConfirmDangerModal phrase for the June 2026 close. */
const PHRASE = "2026-06";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(claims: object): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ ...claims, exp })}.sig`;
}

function closedPeriod(overrides: Partial<PeriodRecord> = {}): PeriodRecord {
  return {
    id: "pppppppp-aaaa-bbbb-cccc-111111111111",
    period_start: "2026-06-01",
    period_end: "2026-06-30",
    status: "closed",
    closed_at: "2026-07-01T08:00:00+00:00",
    version: 1,
    ...overrides,
  };
}

const APPROVER_PERMS = {
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

/** transactions:view ONLY — no approve (no close). */
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

/** No transactions module at all — the context note must render NOTHING. */
const NO_TRANSACTIONS_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "reports", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function grantPermissions(perms: unknown) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountScreen() {
  return render(
    <Providers>
      <PeriodsScreen />
    </Providers>,
  );
}

function mountContextNote() {
  return render(
    <Providers>
      <PeriodContextNote />
    </Providers>,
  );
}

/** Open the close dialog from the toolbar. */
async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Close period…" }));
  return await screen.findByRole("dialog", { name: "Close accounting period" });
}

/** Fill the (only) two dialog fields and submit the entry form. */
async function enterClose(user: ReturnType<typeof userEvent.setup>, year = "2026", month = "June") {
  const dialog = await openDialog(user);
  await user.type(within(dialog).getByLabelText("Year"), year);
  await user.selectOptions(
    within(dialog).getByLabelText("Month"),
    within(dialog).getByRole("option", { name: month }),
  );
  await user.click(within(dialog).getByRole("button", { name: "Close period…" }));
  return dialog;
}

/** Type the byte-identical phrase and return the danger confirm button
 * (ByRole name is a FULL accessible-name match: "Close period" is the
 * danger button, distinct from the form's "Close period…"). */
async function armDanger(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(`Type "${PHRASE}" to confirm`), PHRASE);
  return screen.getByRole("button", { name: "Close period" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt({ sub: ADMIN_ID }), refreshToken: "refresh-1" });
  grantPermissions(APPROVER_PERMS);
  mocked.fetchPeriodsPage.mockResolvedValue({ items: [closedPeriod()], nextCursor: null });
  mocked.closePeriod.mockResolvedValue(closedPeriod());
});

afterEach(() => {
  clearSession();
});

test("register renders the period rows VERBATIM: calendar bounds as dates (never through fmtDateTime), the close-state pill, the close stamp — and the honest absence-of-a-row-is-open note", async () => {
  mountScreen();

  expect(await screen.findByText("2026-06-01 → 2026-06-30")).toBeInTheDocument();
  expect(screen.getByText("Closed")).toBeInTheDocument();
  // The honest design statement: rows exist only for CLOSED months.
  expect(screen.getByText(/A row exists only for a month the books were CLOSED for/)).toBeInTheDocument();
  // No reopen affordance exists anywhere (no reopen contract exists).
  expect(screen.queryByRole("button", { name: /reopen/i })).toBeNull();
});

test("empty state renders the honest all-months-open message; error state renders the list error — garbage can never render as period rows", async () => {
  mocked.fetchPeriodsPage.mockResolvedValue({ items: [], nextCursor: null });
  const { unmount } = mountScreen();
  expect(
    await screen.findByText(
      "No accounting period has been closed yet — every month is open for posting.",
    ),
  ).toBeInTheDocument();
  unmount();

  // Hostile/garbage shapes are rejected by the REAL (unmocked) boundary…
  expect(periodSchema.safeParse({ ...closedPeriod(), status: "frozen" }).success).toBe(false);
  expect(periodSchema.safeParse({ ...closedPeriod(), closed_at: "<b>now</b>" }).success).toBe(false);
  // …so such a page reaches the screen only as a rejected fetch.
  mocked.fetchPeriodsPage.mockRejectedValue(new Error("contract violation at the boundary"));
  mountScreen();
  expect(
    await screen.findByText(/Could not load this list/, undefined, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/2026-06-01/)).toBeNull();
});

test("keyset paging: Load more follows the server cursor VERBATIM (gate 1.3); no filter affordance exists (the contract declares none)", async () => {
  const user = userEvent.setup();
  mocked.fetchPeriodsPage
    .mockResolvedValueOnce({ items: [closedPeriod()], nextCursor: "2026-06-01" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchPeriodsPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchPeriodsPage.mock.calls[0]?.[0]).toBeNull();
  expect(mocked.fetchPeriodsPage.mock.calls[1]?.[0]).toBe("2026-06-01");

  // No filter controls: the list contract declares none.
  expect(screen.queryByLabelText("Status")).toBeNull();
});

test("deny-by-default: a view-only role gets NO close affordance and zero writes are possible", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  mountScreen();

  await screen.findByText("2026-06-01 → 2026-06-30");
  expect(screen.queryByRole("button", { name: "Close period…" })).toBeNull();
  expect(mocked.closePeriod).not.toHaveBeenCalled();
});

test("CLOSE: the dialog's ONLY inputs are year and month (the exact contract body); a triple-clicked typed confirmation produces exactly ONE write with the entered month; the result panel renders the SERVER row verbatim (SPENT affordance)", async () => {
  const user = userEvent.setup();
  let releaseClose: (value: PeriodRecord) => void = () => undefined;
  mocked.closePeriod.mockImplementation(
    () =>
      new Promise<PeriodRecord>((resolve) => {
        releaseClose = resolve;
      }),
  );
  mountScreen();
  await screen.findByText("2026-06-01 → 2026-06-30");

  // STRUCTURAL contract proof (asserted BEFORE the confirmation
  // mounts): exactly one textbox (year) and one combobox (month) — no
  // date, bound, version or figure can even be typed (v1.1 rule 1).
  const dialog = await openDialog(user);
  expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
  expect(within(dialog).getAllByRole("combobox")).toHaveLength(1);
  expect(within(dialog).queryAllByRole("spinbutton")).toHaveLength(0);
  await user.type(within(dialog).getByLabelText("Year"), "2026");
  await user.selectOptions(
    within(dialog).getByLabelText("Month"),
    within(dialog).getByRole("option", { name: "June" }),
  );
  await user.click(within(dialog).getByRole("button", { name: "Close period…" }));

  const confirm = await armDanger(user);
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.closePeriod).toHaveBeenCalledTimes(1);
  expect(mocked.closePeriod.mock.calls[0]?.[0]).toBe(2026);
  expect(mocked.closePeriod.mock.calls[0]?.[1]).toBe(6);

  releaseClose(closedPeriod());

  expect(await screen.findByText("Period closed · books frozen")).toBeInTheDocument();
  // The result is the SERVER's row: bounds + close stamp verbatim.
  const panel = screen.getByRole("status");
  expect(within(panel).getByText("2026-06-01 → 2026-06-30")).toBeInTheDocument();
  // The form is SPENT: no second close can be armed from this panel.
  expect(within(dialog).queryByRole("button", { name: "Close period…" })).toBeNull();
  expect(mocked.closePeriod).toHaveBeenCalledTimes(1);
});

test("CLOSE client validation: an empty entry never reaches the wire — labelled field errors render instead (a11y: FormField wiring)", async () => {
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText("2026-06-01 → 2026-06-30");

  await user.click(screen.getByRole("button", { name: "Close period…" }));
  const dialog = await screen.findByRole("dialog", { name: "Close accounting period" });
  await user.click(within(dialog).getByRole("button", { name: "Close period…" }));

  expect(
    await within(dialog).findByText("Enter a four-digit year between 2000 and 2100."),
  ).toBeInTheDocument();
  expect(within(dialog).getByText("Choose the calendar month.")).toBeInTheDocument();
  // aria wiring: the year input is marked invalid and described by its error.
  expect(within(dialog).getByLabelText("Year")).toHaveAttribute("aria-invalid", "true");
  expect(mocked.closePeriod).not.toHaveBeenCalled();
});

test("CLOSE 409 (not elapsed / already closed): EXACTLY ONE attempt, the shared ConflictBanner, the explicit reload NEVER replays, and the re-entered intent carries a ROTATED Idempotency-Key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.closePeriod.mockRejectedValue(new ApiError(409, "conflict", "corr-x"));
  mountScreen();
  await screen.findByText("2026-06-01 → 2026-06-30");

  await enterClose(user);
  await user.click(await armDanger(user));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.closePeriod).toHaveBeenCalledTimes(1);
  const staleKey = mocked.closePeriod.mock.calls[0]?.[2];
  expect(staleKey).toBeTruthy();

  // Explicit reload: the register refetches; NOTHING is replayed.
  const listCallsBefore = mocked.fetchPeriodsPage.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/the conflicted close was withdrawn, nothing was replayed/),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(mocked.fetchPeriodsPage.mock.calls.length).toBeGreaterThan(listCallsBefore),
  );
  expect(mocked.closePeriod).toHaveBeenCalledTimes(1);

  // The re-entered identical intent after the acknowledged conflict is
  // a NEW intent: the key ROTATES.
  mocked.closePeriod.mockResolvedValue(closedPeriod());
  const dialog = screen.getByRole("dialog", { name: "Close accounting period" });
  await user.click(within(dialog).getByRole("button", { name: "Close period…" }));
  await user.click(await armDanger(user));

  await waitFor(() => expect(mocked.closePeriod).toHaveBeenCalledTimes(2));
  const freshKey = mocked.closePeriod.mock.calls[1]?.[2];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("A5 context note: renders the latest CLOSED month VERBATIM next to the figures operators read, with the honest later-months-open statement and the register link", async () => {
  mocked.fetchPeriodsPage.mockResolvedValue({
    items: [closedPeriod(), closedPeriod({ id: "older", period_start: "2026-05-01", period_end: "2026-05-31" })],
    nextCursor: null,
  });
  mountContextNote();

  expect(await screen.findByText("2026-06-30")).toBeInTheDocument();
  expect(screen.getByText(/later months are open for posting/)).toBeInTheDocument();
  // The older row never leaks into the context line.
  expect(screen.queryByText("2026-05-31")).toBeNull();
  expect(screen.getByRole("link", { name: "View period register" })).toHaveAttribute(
    "href",
    "/modules/transactions/periods",
  );
});

test("A5 context note honesty: the no-closes case is stated explicitly; a read failure renders an explicit 'unavailable' line (never implying everything is open)", async () => {
  mocked.fetchPeriodsPage.mockResolvedValue({ items: [], nextCursor: null });
  const { unmount } = mountContextNote();
  expect(
    await screen.findByText(/no\s+month has been closed yet — every month is open for posting/),
  ).toBeInTheDocument();
  unmount();

  mocked.fetchPeriodsPage.mockRejectedValue(new Error("boundary rejection"));
  mountContextNote();
  expect(
    await screen.findByText(/Accounting-period context is unavailable right now/, undefined, {
      timeout: 5000,
    }),
  ).toBeInTheDocument();
  expect(screen.queryByText(/open for posting/)).toBeNull();
});

test("A5 context note SELF-GATES: a role without transactions:view renders NOTHING and the periods endpoint is NEVER probed", async () => {
  grantPermissions(NO_TRANSACTIONS_PERMS);
  const { container } = mountContextNote();

  // Settle any microtasks — the fetch must never fire.
  await waitFor(() => expect(container.textContent).toBe(""));
  expect(mocked.fetchPeriodsPage).not.toHaveBeenCalled();
});
