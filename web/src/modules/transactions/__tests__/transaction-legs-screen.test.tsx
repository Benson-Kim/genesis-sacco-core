/**
 * Double-entry legs drill-down (#31 ledger (g)) THROUGH THE REAL
 * DRAWER CODE: the legs section of TransactionDetailDrawer mounted
 * under the real <Providers> with the feature API layer stubbed at
 * the module boundary (the transactions reference-suite pattern).
 *
 * The batch's falsifiable claims:
 * - VERBATIM rows: every leg renders the SERVER's account/side/amount
 *   exactly — and the NON-ADDITIVE fixture proves no client-side
 *   balancing total renders (blocker (a)): a 100.00 DR against
 *   40.00 + 60.00 CRs must never produce a gross (200.00), a net
 *   (0.00) or any "Total" row.
 * - Structural withholding: without transactions:view the legs read
 *   is NEVER probed and no legs section mounts (deny-by-default).
 * - XSS: a hostile chart-of-accounts key renders as inert text.
 * - A failed read renders the least-disclosure error banner — no leg,
 *   figure or invented state.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { TransactionDetailDrawer } from "../components/TransactionDetailDrawer";
import type { LedgerLeg, Transaction } from "../schemas";
import * as txnApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchTransactionLegs: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(txnApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const TXN_ID = "aaaaaaaa-1111-2222-3333-444444444444";

// Attacker-influenced server string — the chart-of-accounts key is
// rendered data like any other and must stay inert.
const HOSTILE_ACCOUNT = "<img src=x onerror=window.__pwned=7>evil.account";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

/** Tenant-level posting (member_id null) so the drawer never needs the
 * members module — this suite is about the LEGS section only. */
const TXN: Transaction = {
  id: TXN_ID,
  txn_ref: "RP-0100",
  member_id: null,
  type: "loan_repayment",
  amount: "100.00",
  channel: "mpesa",
  direction: "credit",
  occurred_at: "2026-07-18T10:15:00+00:00",
  is_reversal: false,
  created_by: null,
  external_ref: null,
};

/** NON-ADDITIVE proof fixture: 1 DR balancing 2 CRs. Any client-side
 * "helpful" figure — gross 200.00, net 0.00, a Total row — is exactly
 * what the assertions below forbid. */
const THREE_LEGS: LedgerLeg[] = [
  { account: "cash.mpesa", side: "debit", amount: "100.00" },
  { account: "income.interest", side: "credit", amount: "40.00" },
  { account: "loans.receivable", side: "credit", amount: "60.00" },
];

const TXN_VIEW_PERMS = {
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

/** A role that can somehow reach the drawer WITHOUT transactions:view
 * (e.g. a future cross-module embed): the legs read must be
 * structurally withheld, not merely hidden. */
const NO_TXN_VIEW_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function grantPermissions(perms: typeof TXN_VIEW_PERMS | typeof NO_TXN_VIEW_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountDrawer(txn: Transaction = TXN) {
  return render(
    <Providers>
      <TransactionDetailDrawer txn={txn} onClose={jest.fn()} />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(TXN_VIEW_PERMS);
  mocked.fetchTransactionLegs.mockResolvedValue(THREE_LEGS);
});

afterEach(() => {
  clearSession();
});

test("legs render VERBATIM, one row per leg in server order — and the NON-ADDITIVE fixture proves no client-side balancing total renders (blocker (a))", async () => {
  mountDrawer();

  const table = await screen.findByRole("table");
  const rows = within(table).getAllByRole("row");
  // Header + exactly the three server legs — nothing appended (no
  // footer, no Total row).
  expect(rows).toHaveLength(4);
  // The length assertion above proves the indexes exist (the `!` is
  // for noUncheckedIndexedAccess — the house mock.calls[0]! pattern).
  expect(within(rows[1]!).getByText("cash.mpesa")).toBeInTheDocument();
  expect(within(rows[1]!).getByText("Debit")).toBeInTheDocument();
  expect(within(rows[1]!).getByText("KES 100.00")).toBeInTheDocument();
  expect(within(rows[2]!).getByText("income.interest")).toBeInTheDocument();
  expect(within(rows[2]!).getByText("KES 40.00")).toBeInTheDocument();
  expect(within(rows[3]!).getByText("loans.receivable")).toBeInTheDocument();
  expect(within(rows[3]!).getByText("KES 60.00")).toBeInTheDocument();

  // NON-ADDITIVE proof: neither the gross (200.00) nor the net (0.00)
  // of the legs exists anywhere on the screen, and no element claims
  // to be a total. Add any client-side summing and this fails.
  expect(screen.queryByText(/200\.00/)).toBeNull();
  expect(screen.queryByText(/KES 0\.00/)).toBeNull();
  expect(screen.queryByText(/Total/)).toBeNull();
  expect(screen.queryByText(/ total/)).toBeNull();
  expect(mocked.fetchTransactionLegs).toHaveBeenCalledWith(TXN_ID);
  expect(mocked.fetchTransactionLegs).toHaveBeenCalledTimes(1);
});

test("a hostile chart-of-accounts key renders as inert TEXT (named XSS threat)", async () => {
  mocked.fetchTransactionLegs.mockResolvedValue([
    { account: HOSTILE_ACCOUNT, side: "debit", amount: "100.00" },
    { account: "member.deposits", side: "credit", amount: "100.00" },
  ]);
  const { container } = mountDrawer();

  // The payload is visible as literal text…
  expect(await screen.findByText(/window\.__pwned/)).toBeInTheDocument();
  // …and never materialised as markup.
  expect(container.querySelector("img")).toBeNull();
  expect(
    (window as unknown as Record<string, unknown>)["__pwned"],
  ).toBeUndefined();
});

test("structural withholding: without transactions:view the legs read is NEVER probed and no legs section mounts (deny-by-default, gate 1.6)", async () => {
  grantPermissions(NO_TXN_VIEW_PERMS);
  mountDrawer();

  // The drawer itself renders (it is the caller's witnessed row)…
  expect(await screen.findByRole("dialog", { name: "Transaction detail" })).toBeInTheDocument();
  // …but the legs surface does not exist and the endpoint was never
  // touched — withholding is structural, not cosmetic.
  expect(screen.queryByText("Double-entry legs")).toBeNull();
  expect(screen.queryByRole("table")).toBeNull();
  await waitFor(() => expect(mocked.fetchTransactionLegs).not.toHaveBeenCalled());
});

test("a failed legs read renders the least-disclosure error banner — no invented rows, no figures", async () => {
  mocked.fetchTransactionLegs.mockRejectedValue(new Error("boom"));
  mountDrawer();

  expect(await screen.findByText("Double-entry legs")).toBeInTheDocument();
  // The shared banner's sanitized copy renders… (the Providers default
  // retries queries ONCE with ~1s backoff before surfacing the error
  // state — allow for it, the house pattern).
  expect(
    await screen.findByText("Network error — check connectivity and retry.", undefined, {
      timeout: 5000,
    }),
  ).toBeInTheDocument();
  // …never rows and never a leg figure (the drawer's own witnessed
  // Amount Kv is the ONLY money on screen).
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.queryByText("KES 40.00")).toBeNull();
  expect(screen.queryByText("KES 60.00")).toBeNull();
});
