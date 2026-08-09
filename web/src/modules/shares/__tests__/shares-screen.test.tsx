/**
 * Share-transfers console adversarial tests THROUGH THE REAL SCREEN
 * CODE (issue #31 batch 10, ledger (l)/(m) — the !77 human-authorized
 * maker-checker rework). The feature API layer is stubbed at the
 * module boundary; rendering, permission gating, idempotency-key
 * custody, the typed confirmations, the structural SoD withhold and
 * 409 handling are the real shipped code, mounted under the REAL
 * <Providers>.
 *
 * Centrepieces (the corrections/recovery reference-harness pyramid):
 * - MAKER phase moves NO money: the request success panel states
 *   PENDING and the (m) register refetches — no balance figure exists
 *   anywhere in the request response to render.
 * - REGISTER: the server's pending-first order renders verbatim
 *   (never re-sorted); a row opens the review drawer.
 * - STRUCTURAL SoD (blocker (f)): when the signed-in user IS the
 *   maker, the drawer's approve/reject controls are NEVER mounted
 *   (MakerCheckerPanel withhold) — falsifiable: pass the own id as
 *   maker and assert the banner, not the buttons.
 * - EXACTLY ONE write per intent: typed confirmations start DISARMED;
 *   a triple-clicked confirm posts ONCE; approval's wire body is
 *   EMPTY by contract (the API layer owns it — asserted in the
 *   network suite); rejection REQUIRES the rationale.
 * - MONEY VERBATIM & NON-ADDITIVE (blocker (a)): the approval result
 *   renders the server's three figures exactly; the fixture is
 *   deliberately NON-ADDITIVE and every plausible sum is asserted
 *   ABSENT.
 * - Deny-by-default: members:view-only gets NO request form (the
 *   register still renders — it is the (m) read surface).
 * - XSS inertness on the server-influenced strings (ledger refs).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { ShareTransfersScreen } from "../components/ShareTransfersScreen";
import type { ShareTransferRecord, ShareTransferResult } from "../schemas";
import * as sharesApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    requestShareTransfer: jest.fn(),
    fetchShareTransfersPage: jest.fn(),
    fetchShareTransfer: jest.fn(),
    approveShareTransfer: jest.fn(),
    rejectShareTransfer: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(sharesApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MAKER_ID = "88888888-8888-8888-8888-888888888888";
const FROM_ID = "11111111-1111-1111-1111-111111111111";
const TO_ID = "22222222-2222-2222-2222-222222222222";
const PHRASE = FROM_ID.slice(0, 8);

const PENDING_ID = "aaaabbbb-1111-2222-3333-444444444444";
const POSTED_ID = "ccccdddd-1111-2222-3333-444444444444";

/** The maker's request response / the register's pending row —
 * created by a DIFFERENT officer so the signed-in user is an eligible
 * checker in the drawer tests. */
const PENDING_RECORD: ShareTransferRecord = {
  id: PENDING_ID,
  from_member_id: FROM_ID,
  to_member_id: TO_ID,
  amount: "5000.10",
  status: "pending",
  out_transaction_id: null,
  in_transaction_id: null,
  created_by: MAKER_ID,
  approved_by: null,
  decided_at: null,
  version: 1,
  created_at: "2026-08-07T09:00:00Z",
};

/** Terminal history row behind the pending band (server order). */
const POSTED_RECORD: ShareTransferRecord = {
  id: POSTED_ID,
  from_member_id: FROM_ID,
  to_member_id: TO_ID,
  amount: "120.55",
  status: "posted",
  out_transaction_id: "eeeeffff-1111-2222-3333-444444444444",
  in_transaction_id: "eeeeffff-5555-6666-7777-888888888888",
  created_by: MAKER_ID,
  approved_by: ADMIN_ID,
  decided_at: "2026-08-06T12:00:00Z",
  version: 2,
  created_at: "2026-08-06T11:00:00Z",
};

/**
 * NON-ADDITIVE by construction (blocker (a)): 5000.10 + 15000.25 =
 * 20005.35 ≠ 20000.35; 5000.10 + 20000.35 = 25005.45; 15000.25 +
 * 20000.35 = 35000.60 — none of these sums equals ANY rendered
 * figure, and each is asserted ABSENT from the DOM below.
 */
const APPROVE_RESULT: ShareTransferResult = {
  transfer_id: PENDING_ID,
  out_txn_ref: "ST-OUT-000123",
  in_txn_ref: "ST-IN-000123",
  amount: "5000.10",
  from_balance_after: "15000.25",
  to_balance_after: "20000.35",
};

const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: true, can_edit: true, can_approve: true },
  ],
};

/** members:view ONLY — equity moves are approve-gated; the register
 * read is the (m) surface and stays. */
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

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function mountScreen() {
  return render(
    <Providers>
      <ShareTransfersScreen />
    </Providers>,
  );
}

/** Fill the three-field request form with valid values. */
async function fillEntry(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Transferring member id"), FROM_ID);
  await user.type(screen.getByLabelText("Receiving member id"), TO_ID);
  await user.type(screen.getByLabelText("Amount (KES)"), "5000.10");
}

/** Submit the request form, type the byte-identical phrase, return
 * the danger confirm button. */
async function armRequestConfirm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Request transfer…" }));
  await user.type(await screen.findByLabelText(`Type "${PHRASE}" to confirm`), PHRASE);
  return screen.getByRole("button", { name: "Request transfer" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.requestShareTransfer.mockResolvedValue(PENDING_RECORD);
  mocked.fetchShareTransfersPage.mockResolvedValue({
    items: [PENDING_RECORD, POSTED_RECORD],
    nextCursor: null,
  });
  mocked.fetchShareTransfer.mockResolvedValue(PENDING_RECORD);
  mocked.approveShareTransfer.mockResolvedValue(APPROVE_RESULT);
  mocked.rejectShareTransfer.mockResolvedValue({
    ...PENDING_RECORD,
    status: "rejected",
    approved_by: ADMIN_ID,
    decided_at: "2026-08-07T10:00:00Z",
    version: 2,
  });
});

afterEach(() => {
  clearSession();
});

test("deny-by-default: members:view ONLY gets NO request form (the honest note renders instead) — but the (m) register STILL renders (the read surface)", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  mountScreen();

  expect(await screen.findByText(/no members approval permission/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Transferring member id")).toBeNull();
  expect(screen.queryByRole("button", { name: "Request transfer…" })).toBeNull();
  expect(mocked.requestShareTransfer).not.toHaveBeenCalled();
  // The register is members:view — it renders for this role.
  expect(await screen.findByRole("table")).toBeInTheDocument();
  expect(screen.getByText("Pending approval")).toBeInTheDocument();
});

test("the register renders the SERVER's pending-first order verbatim (never re-sorted): the pending row precedes the older posted row exactly as served", async () => {
  mountScreen();

  const table = await screen.findByRole("table");
  const rows = table.querySelectorAll("tbody tr");
  expect(rows).toHaveLength(2);
  expect(rows[0]?.textContent).toContain("Pending approval");
  expect(rows[0]?.textContent).toContain(PENDING_ID.slice(0, 8));
  expect(rows[1]?.textContent).toContain("Posted");
  expect(rows[1]?.textContent).toContain(POSTED_ID.slice(0, 8));
  // Server figures verbatim in the rows.
  expect(rows[0]?.textContent).toContain("KES 5,000.10");
  expect(rows[1]?.textContent).toContain("KES 120.55");
  expect(mocked.fetchShareTransfersPage).toHaveBeenCalledWith(null);
});

test("MAKER phase: typed confirmation starts DISARMED; a triple-clicked confirm records EXACTLY ONE request; the PENDING panel replaces the form and STATES no money moved (no balance figure exists to render)", async () => {
  const user = userEvent.setup();
  let release: (value: ShareTransferRecord) => void = () => undefined;
  mocked.requestShareTransfer.mockImplementation(
    () =>
      new Promise<ShareTransferRecord>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();
  await fillEntry(user);

  await user.click(screen.getByRole("button", { name: "Request transfer…" }));
  // Disarmed until the byte-identical phrase — a reflex click writes
  // nothing.
  const disarmed = screen.getByRole("button", { name: "Request transfer" });
  expect(disarmed).toBeDisabled();
  expect(mocked.requestShareTransfer).not.toHaveBeenCalled();

  await user.type(screen.getByLabelText(`Type "${PHRASE}" to confirm`), PHRASE);
  const confirm = screen.getByRole("button", { name: "Request transfer" });
  await user.click(confirm);
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(1);
  // The typed decimal STRING travels verbatim — never a float.
  expect(mocked.requestShareTransfer.mock.calls[0]?.[0]).toBe(FROM_ID);
  expect(mocked.requestShareTransfer.mock.calls[0]?.[1]).toBe(TO_ID);
  expect(mocked.requestShareTransfer.mock.calls[0]?.[2]).toBe("5000.10");

  release(PENDING_RECORD);

  // The SPENT panel replaces the form and states PENDING/no-money.
  expect(await screen.findByText(/Transfer request recorded · PENDING approval/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Transferring member id")).toBeNull();
  expect(screen.getByText(/NO money moved/)).toBeInTheDocument();
  expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(1);
});

test("CHECKER phase (eligible checker): a register row opens the drawer; approve is typed-confirmed and posts ONCE; the result renders the server's NON-ADDITIVE figures VERBATIM with no client-side sum anywhere", async () => {
  const user = userEvent.setup();
  mountScreen();

  const table = await screen.findByRole("table");
  const firstRow = table.querySelectorAll("tbody tr")[0]!;
  await user.click(firstRow as HTMLElement);

  // The drawer loads the FRESH record; the maker is a DIFFERENT
  // officer, so checker controls mount (structural SoD).
  expect(await screen.findByText(/Requesting officer 88888888/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Approve & post transfer…" }));
  const drawerPhrase = PENDING_ID.slice(0, 8);
  await user.type(
    await screen.findByLabelText(`Type "${drawerPhrase}" to confirm`),
    drawerPhrase,
  );
  const confirm = screen.getByRole("button", { name: "Approve & post transfer" });
  await user.click(confirm);
  await user.click(confirm);
  expect(mocked.approveShareTransfer).toHaveBeenCalledTimes(1);
  expect(mocked.approveShareTransfer.mock.calls[0]?.[0]).toBe(PENDING_ID);

  // The result panel renders every figure VERBATIM…
  expect(await screen.findByText(/Transfer posted · ST-OUT-000123 \/ ST-IN-000123/)).toBeInTheDocument();
  expect(screen.getByText("KES 15,000.25")).toBeInTheDocument();
  expect(screen.getByText("KES 20,000.35")).toBeInTheDocument();
  // …and NO sum of any two figures anywhere (non-additive proof —
  // falsifiable: render any client-computed sum and this fails).
  expect(screen.queryByText("KES 20,005.35")).toBeNull();
  expect(screen.queryByText("KES 25,005.45")).toBeNull();
  expect(screen.queryByText("KES 35,000.60")).toBeNull();
});

test("STRUCTURAL SoD withhold (blocker (f)): when the signed-in user IS the maker, the drawer NEVER mounts approve/reject controls — the SoD banner renders instead (server enforces regardless)", async () => {
  const user = userEvent.setup();
  mocked.fetchShareTransfer.mockResolvedValue({
    ...PENDING_RECORD,
    created_by: ADMIN_ID,
  });
  mountScreen();

  const table = await screen.findByRole("table");
  await user.click(table.querySelectorAll("tbody tr")[0] as HTMLElement);

  expect(await screen.findByText(/You initiated this action/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve & post transfer…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Reject transfer…" })).toBeNull();
  expect(mocked.approveShareTransfer).not.toHaveBeenCalled();
  expect(mocked.rejectShareTransfer).not.toHaveBeenCalled();
});

test("rejection REQUIRES the rationale (!52 F2): an empty reason never arms the confirm; a valid reason pins the FRESH record version on the wire", async () => {
  const user = userEvent.setup();
  mountScreen();

  const table = await screen.findByRole("table");
  await user.click(table.querySelectorAll("tbody tr")[0] as HTMLElement);
  expect(await screen.findByText(/Requesting officer 88888888/)).toBeInTheDocument();

  // Empty rationale: refused BEFORE any confirmation arms.
  await user.click(screen.getByRole("button", { name: "Reject transfer…" }));
  expect(
    await screen.findByText("The rejection rationale is required (four-eyes practice)."),
  ).toBeInTheDocument();
  expect(mocked.rejectShareTransfer).not.toHaveBeenCalled();

  await user.type(
    screen.getByLabelText("Rejection rationale (required to reject)"),
    "Amount keyed against the wrong member.",
  );
  await user.click(screen.getByRole("button", { name: "Reject transfer…" }));
  const drawerPhrase = PENDING_ID.slice(0, 8);
  await user.type(
    await screen.findByLabelText(`Type "${drawerPhrase}" to confirm`),
    drawerPhrase,
  );
  await user.click(screen.getByRole("button", { name: "Reject transfer" }));
  await waitFor(() => expect(mocked.rejectShareTransfer).toHaveBeenCalledTimes(1));
  // id, FRESH version, the trimmed rationale.
  expect(mocked.rejectShareTransfer.mock.calls[0]?.[0]).toBe(PENDING_ID);
  expect(mocked.rejectShareTransfer.mock.calls[0]?.[1]).toBe(1);
  expect(mocked.rejectShareTransfer.mock.calls[0]?.[2]).toBe(
    "Amount keyed against the wrong member.",
  );
});

test("terminal record: the drawer offers NO checker actions on a posted row (the status machine is server truth)", async () => {
  const user = userEvent.setup();
  mocked.fetchShareTransfer.mockResolvedValue(POSTED_RECORD);
  mountScreen();

  const table = await screen.findByRole("table");
  await user.click(table.querySelectorAll("tbody tr")[1] as HTMLElement);

  expect(await screen.findByText(/a terminal\s+state/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve & post transfer…" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Reject transfer…" })).toBeNull();
});

test("409 on the request (balance shortfall / inactive member): EXACTLY ONE attempt; the explicit acknowledge flow NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.requestShareTransfer
    .mockRejectedValueOnce(new ApiError(409, "conflict", "corr-shortfall"))
    .mockResolvedValue(PENDING_RECORD);
  mountScreen();
  await fillEntry(user);

  await user.click(await armRequestConfirm(user));
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(1);
  const staleKey = mocked.requestShareTransfer.mock.calls[0]?.[3];

  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/Nothing was recorded by the conflicted attempt/),
  ).toBeInTheDocument();
  expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(1);

  // Re-entering the IDENTICAL request after the acknowledged conflict
  // is a NEW intent — rotated key.
  await user.click(await armRequestConfirm(user));
  await waitFor(() => expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(2));
  expect(mocked.requestShareTransfer.mock.calls[1]?.[3]).not.toBe(staleKey);
  expect(hasSession()).toBe(true);
});

test("a legitimate identical SECOND request (Record another) is a NEW intent with a ROTATED key — never served the first request's stored response", async () => {
  const user = userEvent.setup();
  mountScreen();
  await fillEntry(user);
  await user.click(await armRequestConfirm(user));

  expect(await screen.findByText(/Transfer request recorded/)).toBeInTheDocument();
  const firstKey = mocked.requestShareTransfer.mock.calls[0]?.[3];

  await user.click(screen.getByRole("button", { name: "Record another request" }));
  // The form returns EMPTY (a new intent, not a prefilled replay).
  expect(screen.getByLabelText("Transferring member id")).toHaveValue("");
  await fillEntry(user);
  await user.click(await armRequestConfirm(user));
  await waitFor(() => expect(mocked.requestShareTransfer).toHaveBeenCalledTimes(2));
  expect(mocked.requestShareTransfer.mock.calls[1]?.[3]).not.toBe(firstKey);
});

test("XSS inertness: hostile ledger refs in the approval response render as inert TEXT (no element injection)", async () => {
  const user = userEvent.setup();
  mocked.approveShareTransfer.mockResolvedValue({
    ...APPROVE_RESULT,
    out_txn_ref: "<img src=x onerror=window.__pwned=1>",
    in_txn_ref: "<script>window.__pwned=2</script>",
  });
  const { container } = mountScreen();

  const table = await screen.findByRole("table");
  await user.click(table.querySelectorAll("tbody tr")[0] as HTMLElement);
  expect(await screen.findByText(/Requesting officer 88888888/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Approve & post transfer…" }));
  const drawerPhrase = PENDING_ID.slice(0, 8);
  await user.type(
    await screen.findByLabelText(`Type "${drawerPhrase}" to confirm`),
    drawerPhrase,
  );
  await user.click(screen.getByRole("button", { name: "Approve & post transfer" }));

  expect(await screen.findByText(/Transfer posted/)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});
