/**
 * Reports catalogue + export request/queue/download adversarial tests
 * THROUGH THE REAL SCREEN CODE (P15 Reports batch), mirroring the
 * transactions/loans reference suites: the feature API layer is
 * stubbed at the module boundary; rendering, permission gating,
 * idempotency-key custody, 409 handling, the witnessed registry and
 * its session teardown are the real shipped code, mounted under the
 * REAL <Providers>.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { ReportsScreen } from "../components/ReportsScreen";
import { clearWitnessedExports, recordWitnessedExport } from "../exportRegistry";
import {
  validateExportDraft,
  isCalendarDate,
  EMPTY_FILTER_DRAFT,
  type ExportOut,
} from "../schemas";
import * as reportsApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    requestExport: jest.fn(),
    fetchExport: jest.fn(),
    runExportQueue: jest.fn(),
    downloadArtifact: jest.fn(),
    fetchExitsPage: jest.fn(),
    fetchDeclarationsPage: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(reportsApi);
const mockedMembers = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXPORT_ID = "eeeeeeee-1111-2222-3333-444444444444";

// Attacker-influenced free-text fields on this screen — must render as
// inert text (named XSS threat).
const HOSTILE_REPORT = "<script>window.__pwned=3</script>weird_report";
const HOSTILE_STATUS = "<img src=x onerror=window.__pwned=4>queued?";
const HOSTILE_COLUMN = '<b>"member_no"</b>';
// F-R5: the server's filters map renders as key=value pairs — BOTH
// sides are attacker-influenced and must stay inert text.
const HOSTILE_FILTER_KEY = "<svg onload=window.__pwned=5>scope";
const HOSTILE_FILTER_VALUE = '"><iframe src="javascript:window.__pwned=6"></iframe>';

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function exportOut(overrides: Partial<ExportOut> = {}): ExportOut {
  return {
    id: EXPORT_ID,
    report: "member_statement",
    status: "queued",
    filters: { member_id: MEMBER_ID },
    allowed_columns: ["member_no", "date", "txn_ref", "type"],
    as_of: "2026-08-04T10:00:00+00:00",
    completed_at: null,
    created_at: "2026-08-04T10:00:00+00:00",
    artifact: null,
    ...overrides,
  };
}

const COMPLETED_EXPORT: ExportOut = exportOut({
  status: "completed",
  completed_at: "2026-08-04T10:05:00+00:00",
  artifact: {
    row_count: 3,
    truncated: true,
    row_limit: 10000,
    expires_at: "2026-08-05T12:00:00+00:00",
    csv_download: "/exports/downloads/csv-Tok_en-1",
    pdf_download: "/exports/downloads/pdf-Tok_en-1",
  },
});

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
    { module: "reports", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "transactions", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** reports:view ONLY — no members, no transactions grant. */
const REPORTS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "reports", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS | typeof REPORTS_ONLY_PERMS) {
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
      <ReportsScreen />
    </Providers>,
  );
}

/** The catalogue card for one report title → its request button. */
function requestButtonFor(title: string): HTMLElement {
  const card = screen.getByText(title).parentElement;
  if (card === null) throw new Error(`no card for ${title}`);
  return within(card).getByRole("button", { name: "Request export…" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearWitnessedExports();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.requestExport.mockResolvedValue(exportOut());
  mocked.fetchExport.mockResolvedValue(COMPLETED_EXPORT);
  mocked.runExportQueue.mockResolvedValue({ completed: 2, failed: 0 });
  mocked.downloadArtifact.mockResolvedValue(undefined);
  mocked.fetchExitsPage.mockResolvedValue(
    page([
      {
        id: "dddddddd-1111-2222-3333-444444444444",
        member_id: MEMBER_ID,
        status: "requested",
        created_at: "2026-07-01T09:00:00+00:00",
      },
    ]),
  );
  mocked.fetchDeclarationsPage.mockResolvedValue(
    page([
      {
        id: "cccccccc-1111-2222-3333-444444444444",
        fy_start: "2025-01-01",
        fy_end: "2025-12-31",
        status: "declared",
      },
    ]),
  );
  mockedMembers.fetchMembersPage.mockResolvedValue(page([MEMBER]));
});

afterEach(() => {
  clearWitnessedExports();
  clearSession();
});

test("the catalogue offers EXACTLY the contract's 11 reports with their declared scopes — nothing invented, nothing dropped", async () => {
  mountScreen();
  expect(await screen.findByText("Member statement")).toBeInTheDocument();
  expect(screen.getByText("Trial balance")).toBeInTheDocument();
  expect(screen.getByText("Loan book — classification & provisions")).toBeInTheDocument();
  expect(screen.getByText("SASRA return (skeleton)")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Request export…" })).toHaveLength(11);
  // The session panel is honest about its per-tab scope.
  expect(screen.getByText(/no export list endpoint/)).toBeInTheDocument();
  expect(screen.getByText(/No exports requested in this session yet/)).toBeInTheDocument();
});

test("hostile report/status/column strings AND a hostile filters map from the server render as inert TEXT everywhere (named XSS threat, F-R5)", async () => {
  const user = userEvent.setup();
  mocked.requestExport.mockResolvedValue(
    exportOut({
      report: HOSTILE_REPORT,
      status: HOSTILE_STATUS,
      allowed_columns: [HOSTILE_COLUMN],
      // F-R5: hostile KEYS and VALUES in the server's filters record —
      // the result panel renders them verbatim as key=value pairs.
      filters: { [HOSTILE_FILTER_KEY]: HOSTILE_FILTER_VALUE },
    }),
  );
  const { container } = mountScreen();

  // Trial balance takes no filters — a one-click request.
  await user.click(requestButtonFor("Trial balance"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));

  // The result panel renders the server's hostile status and column
  // strings as literal text…
  expect(await within(drawer).findByText(new RegExp("onerror=window.__pwned"))).toBeInTheDocument();
  expect(within(drawer).getByText(new RegExp('"member_no"'))).toBeInTheDocument();
  // …and the hostile filters map renders as one literal key=value line
  // (both sides inert — neither the <svg> key nor the javascript:
  // iframe value ever parses as markup)…
  expect(
    within(drawer).getByText(new RegExp("onload=window.__pwned=5.*scope=")),
  ).toBeInTheDocument();
  expect(within(drawer).getByText(new RegExp("window.__pwned=6"))).toBeInTheDocument();
  // Two controls in the open drawer share the accessible name "Close"
  // (the header ✕ and the result panel's action) — scope to the result
  // panel to name the action button unambiguously.
  const resultPanel = within(drawer).getByRole("status");
  await user.click(within(resultPanel).getByRole("button", { name: "Close" }));
  // …and the witnessed row renders the unrecognised hostile report name
  // as literal text (the label fallback never interprets it)…
  expect(screen.getByText(new RegExp("window.__pwned=3"))).toBeInTheDocument();
  // …and never materialise as markup.
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("b")).toBeNull();
  expect(container.querySelector("svg")).toBeNull();
  expect(container.querySelector("iframe")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("DOUBLE-SUBMIT impossible from this client: a triple-clicked request produces exactly ONE write; the affordance is SPENT after success", async () => {
  const user = userEvent.setup();
  let release: (value: ExportOut) => void = () => undefined;
  mocked.requestExport.mockImplementation(
    () =>
      new Promise<ExportOut>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await user.click(requestButtonFor("Trial balance"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  const submit = within(drawer).getByRole("button", { name: "Request export" });
  await user.click(submit);
  await user.click(submit);
  await user.click(submit);

  expect(mocked.requestExport).toHaveBeenCalledTimes(1);
  expect(mocked.requestExport.mock.calls[0]?.[0]).toEqual({
    report: "trial_balance",
    filters: {},
  });

  release(exportOut({ report: "trial_balance", filters: {} }));

  // The result panel replaces the form — the affordance is spent…
  expect(await within(drawer).findByText(/Export queued · Trial balance/)).toBeInTheDocument();
  expect(within(drawer).getByText("whole tenant")).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Request export" })).toBeNull();
  // …still exactly one write, and the witnessed row appeared.
  expect(mocked.requestExport).toHaveBeenCalledTimes(1);
});

test("idempotency keys: STABLE across retries of an identical intent, ROTATED when the scope changes, ROTATED again after 'Request another' (T2)", async () => {
  const user = userEvent.setup();
  mocked.requestExport
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(exportOut());
  mountScreen();

  await user.click(requestButtonFor("Member statement"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  const submit = within(drawer).getByRole("button", { name: "Request export" });

  // Attempt 1 fails with a 5xx…
  await user.click(submit);
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(1));
  // …identical retry (same scope) REUSES the key…
  await user.click(submit);
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(2));
  const key1 = mocked.requestExport.mock.calls[0]?.[1];
  expect(mocked.requestExport.mock.calls[1]?.[1]).toBe(key1);

  // …a different intent (added date scope) ROTATES the key…
  await user.type(within(drawer).getByLabelText("From date"), "2026-07-01");
  await user.click(submit);
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(3));
  const key3 = mocked.requestExport.mock.calls[2]?.[1];
  expect(key3).not.toBe(key1);

  // …and after SUCCESS, an identical 'Request another' is a NEW export
  // job with a NEW key — the server can never dedup the repeat into the
  // first response (review T2).
  await within(drawer).findByText(/Export queued/);
  await user.click(within(drawer).getByRole("button", { name: "Request another" }));
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  await user.type(within(drawer).getByLabelText("From date"), "2026-07-01");
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(4));
  expect(mocked.requestExport.mock.calls[3]?.[0]).toEqual(mocked.requestExport.mock.calls[2]?.[0]);
  expect(mocked.requestExport.mock.calls[3]?.[1]).not.toBe(key3);
});

test("a 409 is a CREATE-style conflict: sanitized banner + operator note, EXACTLY ONE attempt, nothing to reload; the acknowledged retry carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.requestExport
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-c"))
    .mockResolvedValue(exportOut({ report: "trial_balance", filters: {} }));
  mountScreen();

  await user.click(requestButtonFor("Trial balance"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));

  expect(
    await within(drawer).findByText(/The record changed or conflicts with existing data/),
  ).toBeInTheDocument();
  expect(within(drawer).getByText(/Nothing was queued twice/)).toBeInTheDocument();
  // No record-reload affordance exists for this unversioned create.
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.requestExport).toHaveBeenCalledTimes(1);
  const staleKey = mocked.requestExport.mock.calls[0]?.[1];

  // The operator explicitly retries: a NEW intent — the conflict epoch
  // rotates the key so the backend idempotency store pinned to the
  // conflicted claim can never serve its stale outcome.
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(2));
  expect(mocked.requestExport.mock.calls[1]?.[1]).not.toBe(staleKey);
});

test("client validation fires BEFORE any write (required member, UUID and date shapes); the server 422 verdict then WINS per field", async () => {
  const user = userEvent.setup();
  grantPermissions(REPORTS_ONLY_PERMS);
  mocked.requestExport.mockRejectedValue(
    new ApiError(422, "validation_error", null, [
      { field: "member_id", message: "member does not exist in this tenant" },
    ]),
  );
  mountScreen();

  await user.click(requestButtonFor("Member statement"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });

  // Required member missing — zero writes.
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  expect(
    await within(drawer).findByText("Select or enter the member (a UUID)."),
  ).toBeInTheDocument();
  expect(mocked.requestExport).not.toHaveBeenCalled();

  // Malformed UUID — still zero writes.
  await user.type(within(drawer).getByLabelText("Member"), "not-a-uuid");
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  expect(
    await within(drawer).findByText("Select or enter the member (a UUID)."),
  ).toBeInTheDocument();
  expect(mocked.requestExport).not.toHaveBeenCalled();

  // Valid shape: the write happens; the server's field verdict renders
  // inline and WINS.
  await user.clear(within(drawer).getByLabelText("Member"));
  await user.type(within(drawer).getByLabelText("Member"), MEMBER_ID);
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  expect(
    await within(drawer).findByText("member does not exist in this tenant"),
  ).toBeInTheDocument();
  expect(mocked.requestExport).toHaveBeenCalledTimes(1);
});

test("UI affordances follow the matrix: a reports-only role gets NO pickers and ZERO members/exits/declarations fetches — UUID fallbacks instead", async () => {
  grantPermissions(REPORTS_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  // member_statement: UUID field, not the members picker.
  await user.click(requestButtonFor("Member statement"));
  let drawer = await screen.findByRole("dialog", { name: "Request export" });
  expect(within(drawer).getByLabelText("Member")).toHaveProperty("tagName", "INPUT");
  expect(within(drawer).getByText(/cannot list these records/)).toBeInTheDocument();
  await user.click(within(drawer).getByRole("button", { name: "Cancel" }));

  // member_exit_statement: UUID field, not the exits picker.
  await user.click(requestButtonFor("Member exit statement"));
  drawer = await screen.findByRole("dialog", { name: "Request export" });
  expect(within(drawer).getByLabelText("Exit request")).toHaveProperty("tagName", "INPUT");
  await user.click(within(drawer).getByRole("button", { name: "Cancel" }));

  // dividend_rebate_schedule: UUID field, not the declarations picker.
  await user.click(requestButtonFor("Dividend & rebate schedule"));
  drawer = await screen.findByRole("dialog", { name: "Request export" });
  expect(within(drawer).getByLabelText("Dividend declaration")).toHaveProperty("tagName", "INPUT");

  // Structural: the stripped role never even FETCHES the lists.
  expect(mockedMembers.fetchMembersPage).not.toHaveBeenCalled();
  expect(mocked.fetchExitsPage).not.toHaveBeenCalled();
  expect(mocked.fetchDeclarationsPage).not.toHaveBeenCalled();
});

test("with the grants present, the exit and declaration pickers ride their keyset reads and submit the picked ids", async () => {
  const user = userEvent.setup();
  mocked.requestExport.mockResolvedValue(exportOut({ report: "member_exit_statement" }));
  mountScreen();

  await user.click(requestButtonFor("Member exit statement"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.selectOptions(
    await within(drawer).findByLabelText("Exit request"),
    "dddddddd-1111-2222-3333-444444444444",
  );
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));
  await waitFor(() => expect(mocked.requestExport).toHaveBeenCalledTimes(1));
  expect(mocked.requestExport.mock.calls[0]?.[0]).toEqual({
    report: "member_exit_statement",
    filters: { exit_id: "dddddddd-1111-2222-3333-444444444444" },
  });
  expect(mocked.fetchExitsPage).toHaveBeenCalled();
});

test("least-disclosure: a 403 on the request renders the sanitized banner only, session intact", async () => {
  const user = userEvent.setup();
  mocked.requestExport.mockRejectedValue(new ApiError(403, "forbidden", "corr-f"));
  mountScreen();

  await user.click(requestButtonFor("Trial balance"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.click(within(drawer).getByRole("button", { name: "Request export" }));

  expect(await within(drawer).findByText(/Not permitted\./)).toBeInTheDocument();
  expect(within(drawer).getByText(/ref: corr-f/)).toBeInTheDocument();
  expect(within(drawer).queryByText(/forbidden/i)).toBeNull();
  expect(mocked.requestExport).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(true);
});

test("witnessed row: Refresh re-reads the requester-only record; the SERVER's truncation metadata renders verbatim; CSV/PDF ride downloadArtifact with the contract's paths", async () => {
  const user = userEvent.setup();
  recordWitnessedExport(exportOut());
  mountScreen();

  // The queued row shows no downloads yet.
  expect(await screen.findByText("Queued")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "CSV" })).toBeNull();

  await user.click(screen.getByRole("button", { name: "Refresh" }));
  await waitFor(() => expect(mocked.fetchExport).toHaveBeenCalledWith(EXPORT_ID));

  // The refreshed record replaces the row: completed + artifact facts.
  expect(await screen.findByText("Completed")).toBeInTheDocument();
  expect(screen.getByText("3 (truncated at the server cap of 10000)")).toBeInTheDocument();
  expect(screen.getByText(/Download links expire/)).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "CSV" }));
  await waitFor(() => expect(mocked.downloadArtifact).toHaveBeenCalledTimes(1));
  expect(mocked.downloadArtifact.mock.calls[0]).toEqual([
    "/exports/downloads/csv-Tok_en-1",
    "member_statement-2026-08-04.csv",
  ]);
  await user.click(screen.getByRole("button", { name: "PDF" }));
  await waitFor(() => expect(mocked.downloadArtifact).toHaveBeenCalledTimes(2));
  expect(mocked.downloadArtifact.mock.calls[1]).toEqual([
    "/exports/downloads/pdf-Tok_en-1",
    "member_statement-2026-08-04.pdf",
  ]);
});

test("F-R1: a hostile report string can NEVER reach the download filename — the stem is the recognised enum value or the fixed 'export' fallback", async () => {
  const user = userEvent.setup();
  // Path traversal + RTL override (U+202E) + markup — the raw server
  // string must never be handed to the download layer as a filename.
  const HOSTILE_FILENAME_REPORT = "../..\u202Evsc.csv<img>";
  recordWitnessedExport({
    ...COMPLETED_EXPORT,
    report: HOSTILE_FILENAME_REPORT,
  });
  mountScreen();

  expect(await screen.findByText("Completed")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "CSV" }));
  await waitFor(() => expect(mocked.downloadArtifact).toHaveBeenCalledTimes(1));
  expect(mocked.downloadArtifact.mock.calls[0]).toEqual([
    "/exports/downloads/csv-Tok_en-1",
    "export-2026-08-04.csv",
  ]);
  await user.click(screen.getByRole("button", { name: "PDF" }));
  await waitFor(() => expect(mocked.downloadArtifact).toHaveBeenCalledTimes(2));
  expect(mocked.downloadArtifact.mock.calls[1]).toEqual([
    "/exports/downloads/pdf-Tok_en-1",
    "export-2026-08-04.pdf",
  ]);
  // Belt and braces: NO filename ever handed to the download layer
  // contains a separator, an RTL override or markup.
  for (const call of mocked.downloadArtifact.mock.calls) {
    const filename = call[1];
    expect(filename).toMatch(/^[a-z_]+-\d{4}-\d{2}-\d{2}\.(?:csv|pdf)$/);
    expect(filename).not.toContain("/");
    expect(filename).not.toContain("\\");
    expect(filename).not.toContain("\u202E");
    expect(filename).not.toContain("<");
  }
});

test("queue run: one write through the dialog, double-clicked Run runs ONCE, the SERVER's counts render verbatim, the affordance is SPENT", async () => {
  const user = userEvent.setup();
  let release: (value: { completed: number; failed: number }) => void = () => undefined;
  mocked.runExportQueue.mockImplementation(
    () =>
      new Promise<{ completed: number; failed: number }>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  await user.click(screen.getByRole("button", { name: "Run export queue" }));
  const dialog = await screen.findByRole("dialog", { name: "Run export queue" });
  const run = within(dialog).getByRole("button", { name: "Run queue" });
  await user.click(run);
  await user.click(run);

  expect(mocked.runExportQueue).toHaveBeenCalledTimes(1);
  release({ completed: 2, failed: 0 });

  expect(await within(dialog).findByText("Export queue drained")).toBeInTheDocument();
  expect(within(dialog).getByText("Jobs completed").nextSibling?.textContent).toBe("2");
  // Spent: the action is replaced by the result panel.
  expect(within(dialog).queryByRole("button", { name: "Run queue" })).toBeNull();
  expect(mocked.runExportQueue).toHaveBeenCalledTimes(1);
});

test("F-R2 queue-run key custody: STABLE across a pure retry; a second EXPLICIT drain rotates the key — same mount ('Run again') AND remount — so a backend store can never replay the first drain's counts", async () => {
  const user = userEvent.setup();
  mocked.runExportQueue
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-q1"))
    .mockResolvedValue({ completed: 2, failed: 0 });
  mountScreen();

  await user.click(screen.getByRole("button", { name: "Run export queue" }));
  const dialog = await screen.findByRole("dialog", { name: "Run export queue" });

  // Attempt 1 fails with a 5xx…
  await user.click(within(dialog).getByRole("button", { name: "Run queue" }));
  await waitFor(() => expect(mocked.runExportQueue).toHaveBeenCalledTimes(1));
  const key1 = mocked.runExportQueue.mock.calls[0]?.[0];
  expect(key1).toBeTruthy();

  // …the pure retry of the SAME intent REUSES the key…
  await user.click(within(dialog).getByRole("button", { name: "Run queue" }));
  await waitFor(() => expect(mocked.runExportQueue).toHaveBeenCalledTimes(2));
  expect(mocked.runExportQueue.mock.calls[1]?.[0]).toBe(key1);

  // …the drain succeeds; a second EXPLICIT drain in the SAME mount
  // ('Run again') is a NEW intent — the key ROTATES at the wire.
  expect(await within(dialog).findByText("Export queue drained")).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Run again" }));
  await user.click(within(dialog).getByRole("button", { name: "Run queue" }));
  await waitFor(() => expect(mocked.runExportQueue).toHaveBeenCalledTimes(3));
  const key3 = mocked.runExportQueue.mock.calls[2]?.[0];
  expect(key3).toBeTruthy();
  expect(key3).not.toBe(key1);

  // …and a REMOUNT (close + fresh dialog) is also a new intent with a
  // new key slot — rotated again.
  expect(await within(dialog).findByText("Export queue drained")).toBeInTheDocument();
  const resultPanel = within(dialog).getByRole("status");
  await user.click(within(resultPanel).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Run export queue" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Run export queue" }));
  const remounted = await screen.findByRole("dialog", { name: "Run export queue" });
  await user.click(within(remounted).getByRole("button", { name: "Run queue" }));
  await waitFor(() => expect(mocked.runExportQueue).toHaveBeenCalledTimes(4));
  const key4 = mocked.runExportQueue.mock.calls[3]?.[0];
  expect(key4).toBeTruthy();
  expect(key4).not.toBe(key3);
  expect(key4).not.toBe(key1);
});

test("F-R2 queue-run 409: CREATE-style conflict — sanitized banner + operator note, EXACTLY ONE attempt, nothing to reload; the acknowledged retry carries a ROTATED key (!56 F-B4 / !60 F3)", async () => {
  const user = userEvent.setup();
  mocked.runExportQueue
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-qc"))
    .mockResolvedValue({ completed: 1, failed: 0 });
  mountScreen();

  await user.click(screen.getByRole("button", { name: "Run export queue" }));
  const dialog = await screen.findByRole("dialog", { name: "Run export queue" });
  await user.click(within(dialog).getByRole("button", { name: "Run queue" }));

  expect(
    await within(dialog).findByText(/The record changed or conflicts with existing data/),
  ).toBeInTheDocument();
  expect(within(dialog).getByText(/Nothing ran twice/)).toBeInTheDocument();
  // No record-reload affordance exists for this recordless conflict.
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  expect(mocked.runExportQueue).toHaveBeenCalledTimes(1);
  const staleKey = mocked.runExportQueue.mock.calls[0]?.[0];

  // The operator explicitly retries after the acknowledged conflict: a
  // NEW intent — the conflict epoch rotates the key so a backend store
  // pinned to the conflicted claim can never serve its stale outcome.
  await user.click(within(dialog).getByRole("button", { name: "Run queue" }));
  await waitFor(() => expect(mocked.runExportQueue).toHaveBeenCalledTimes(2));
  expect(mocked.runExportQueue.mock.calls[1]?.[0]).not.toBe(staleKey);
  expect(await within(dialog).findByText("Export queue drained")).toBeInTheDocument();
});

test("W56-3: a stray overlay click never discards a dirty export request or the armed queue dialog; the explicit ✕ still closes", async () => {
  const user = userEvent.setup();
  const { container } = mountScreen();

  await user.click(requestButtonFor("Member statement"));
  const drawer = await screen.findByRole("dialog", { name: "Request export" });
  await user.selectOptions(await within(drawer).findByLabelText("Member"), MEMBER_ID);
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Request export" })).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Member")).toHaveValue(MEMBER_ID);
  await user.click(within(drawer).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Request export" })).toBeNull();

  await user.click(screen.getByRole("button", { name: "Run export queue" }));
  const dialog = await screen.findByRole("dialog", { name: "Run export queue" });
  await user.click(container.querySelector('[role="presentation"]') as HTMLElement);
  expect(screen.getByRole("dialog", { name: "Run export queue" })).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.queryByRole("dialog", { name: "Run export queue" })).toBeNull();
});

test("a 401 tears the session down AND clears the witnessed registry (dual-cache + session-scoped store teardown — !60 F2/W58-2)", async () => {
  const user = userEvent.setup();
  recordWitnessedExport(exportOut());
  mocked.fetchExport.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-q"));
  mountScreen();

  expect(await screen.findByText("Queued")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh" }));

  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
  // The witnessed export (id, scope, download paths) died WITH the
  // session — the next operator inherits nothing.
  await waitFor(() => expect(screen.queryByText("Queued")).toBeNull());
  expect(screen.queryByText(EXPORT_ID)).toBeNull();
  expect(screen.getByText(/No exports requested in this session yet/)).toBeInTheDocument();
});

test("validateExportDraft: declared filters only, required enforcement, shape checks, ordered ranges — pure and falsifiable", () => {
  // Required member missing.
  expect(validateExportDraft("member_statement", EMPTY_FILTER_DRAFT).errors).not.toBeNull();
  // Malformed UUID and malformed date.
  expect(
    validateExportDraft("member_statement", {
      ...EMPTY_FILTER_DRAFT,
      member_id: "not-a-uuid",
    }).errors,
  ).not.toBeNull();
  expect(
    validateExportDraft("member_statement", {
      ...EMPTY_FILTER_DRAFT,
      member_id: MEMBER_ID,
      date_from: "18/07/2026",
    }).errors,
  ).not.toBeNull();
  // Impossible calendar date — right SHAPE, no such day (F-R3).
  expect(
    validateExportDraft("member_statement", {
      ...EMPTY_FILTER_DRAFT,
      member_id: MEMBER_ID,
      date_from: "2026-02-31",
    }).errors,
  ).not.toBeNull();
  // Reversed date range.
  expect(
    validateExportDraft("income_statement", {
      ...EMPTY_FILTER_DRAFT,
      date_from: "2026-07-31",
      date_to: "2026-07-01",
    }).errors,
  ).not.toBeNull();
  // A valid draft yields ONLY the provided keys — a stray id staged for
  // another report never leaks into a no-filter report's entry.
  const valid = validateExportDraft("trial_balance", {
    ...EMPTY_FILTER_DRAFT,
    member_id: MEMBER_ID,
  });
  expect(valid.errors).toBeNull();
  expect(valid.entry).toEqual({ report: "trial_balance", filters: {} });
  const scoped = validateExportDraft("member_statement", {
    ...EMPTY_FILTER_DRAFT,
    member_id: MEMBER_ID,
    date_from: "2026-07-01",
  });
  expect(scoped.errors).toBeNull();
  expect(scoped.entry).toEqual({
    report: "member_statement",
    filters: { member_id: MEMBER_ID, date_from: "2026-07-01" },
  });
});

test("isCalendarDate (F-R3): a PURE-STRING accept/reject matrix — impossible calendar dates rejected, leap years computed arithmetically, no Date()", () => {
  const accepted = [
    "2026-01-01",
    "2026-02-28",
    "2024-02-29", // divisible by 4 — leap
    "2000-02-29", // century divisible by 400 — leap
    "2026-04-30",
    "2026-07-18",
    "2026-12-31",
  ];
  const rejected = [
    "2026-02-29", // 2026 is not a leap year
    "1900-02-29", // century NOT divisible by 400 — not a leap year
    "2026-02-31", // no such February day, ever
    "2026-04-31", // April has 30 days
    "2026-13-01", // no 13th month
    "2026-00-10", // no 0th month
    "2026-06-00", // no 0th day
    "2026-1-1", // wrong shape (unpadded)
    "18/07/2026", // wrong shape entirely
    "2026-02-28T00:00:00", // datetime, not a date
    "",
  ];
  // Assert per-value (filter form keeps failures readable).
  expect(accepted.filter((value) => !isCalendarDate(value))).toEqual([]);
  expect(rejected.filter((value) => isCalendarDate(value))).toEqual([]);
});
