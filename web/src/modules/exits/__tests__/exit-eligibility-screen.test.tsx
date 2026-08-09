/**
 * U6 advisory exit-eligibility checklist — SCREEN legs (P15 batch 5,
 * audit #30 U6: the prototype's vExit() criteria rows) through the
 * REAL RequestExitDrawer under the REAL <Providers>; the feature API
 * layers are stubbed at the module boundary.
 *
 * Legs:
 *  - CHECKLIST RENDER: the server's facts render VERBATIM before
 *    submission — counts and booleans only, NO amount anywhere, the
 *    lock-free/re-verdict caveat present; the write fires only AFTER
 *    the checklist was on screen.
 *  - SELF-GATING: a loaded advisory "blocked" verdict withholds the
 *    submit affordance (falsifiable both ways: eligible ⇒ enabled);
 *    the verdict rendered is the SERVER's advisory_eligible — never a
 *    client-side conjunction of the rows.
 *  - ADVISORY-ONLY DEGRADATION: a FAILED eligibility read never gates
 *    — the honest note renders and the request path stays open (the
 *    server enforces every blocker under locks regardless).
 *  - XSS INERTNESS: a hostile member_status string renders as inert
 *    TEXT — no markup materialises (defence in depth behind the
 *    reject-unknowns Zod boundary).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { RequestExitDrawer } from "../components/RequestExitDrawer";
import type { ExitEligibility, ExitRecord } from "../schemas";
import * as exitsApi from "../api";
import * as membersApi from "@/modules/members/api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchExitEligibility: jest.fn(),
    createExitRequest: jest.fn(),
  };
});

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

const mocked = jest.mocked(exitsApi);
const mockedMembers = jest.mocked(membersApi);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const EXIT_ID = "eeeeeeee-aaaa-bbbb-cccc-444444444444";

const HOSTILE_STATUS = '<img src=x onerror=window.__pwned=1> active';

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

/** Distinct counts so each row's VERBATIM value is unambiguous. */
const ELIGIBLE: ExitEligibility = {
  member_id: MEMBER_ID,
  member_status: "active",
  status_allows_exit: true,
  open_exit_exists: false,
  live_guarantees_count: 0,
  open_applications_count: 0,
  active_loans_count: 2,
  unresolved_writeoff_claim: false,
  advisory_eligible: true,
  as_of: "2026-08-06T12:00:00+00:00",
};

const BLOCKED: ExitEligibility = {
  ...ELIGIBLE,
  member_status: "dormant",
  status_allows_exit: true,
  open_exit_exists: true,
  live_guarantees_count: 3,
  open_applications_count: 4,
  unresolved_writeoff_claim: true,
  advisory_eligible: false,
};

const CREATED: ExitRecord = {
  id: EXIT_ID,
  member_id: MEMBER_ID,
  status: "requested",
  reason: null,
  requested_by: null,
  shares_amount: "25000.10",
  deposits_amount: "150000.10",
  loan_balance: "40000.00",
  fees: "500.00",
  net_payable: "134500.20",
  decided_at: null,
  settled_at: null,
  settlement_txn_id: null,
  version: 1,
  created_at: "2026-08-01T09:00:00+00:00",
};

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(claims: object): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ ...claims, exp })}.sig`;
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt({ sub: ADMIN_ID }), refreshToken: "refresh-1" });
  mockedMembers.fetchMembersPage.mockResolvedValue({ items: [MEMBER], nextCursor: null });
  mockedMembers.fetchMember.mockResolvedValue(MEMBER);
  mocked.fetchExitEligibility.mockResolvedValue(ELIGIBLE);
  mocked.createExitRequest.mockResolvedValue(CREATED);
});

afterEach(() => {
  clearSession();
});

function mountDrawer() {
  return render(
    <Providers>
      <RequestExitDrawer onClose={jest.fn()} />
    </Providers>,
  );
}

/** Select the (only) member and wait for the checklist to land. The
 * option list is async (keyset page) — wait for it before selecting. */
async function selectMember(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("option", { name: /Jane Wanjiku/ });
  await user.selectOptions(screen.getByLabelText("Member"), MEMBER_ID);
  return await screen.findByTestId("eligibility-checklist");
}

/** The value <span> of a Kv row, located by its exact label text. */
function kvValue(checklist: HTMLElement, label: string): string {
  const labelNode = within(checklist).getByText(label);
  return labelNode.nextElementSibling?.textContent ?? "";
}

test("checklist render: the server's facts render VERBATIM before submission — counts/booleans only, NO amount, the lock-free re-verdict caveat present; the write fires only after the checklist was shown", async () => {
  const user = userEvent.setup();
  mountDrawer();

  const checklist = await selectMember(user);

  // The advisory read went out for the chosen member, exactly once.
  expect(mocked.fetchExitEligibility).toHaveBeenCalledTimes(1);
  expect(mocked.fetchExitEligibility).toHaveBeenCalledWith(MEMBER_ID);

  // Every row is the SERVER's fact, verbatim (informational active
  // loans included) — counts as bare integers, booleans as their
  // honest labels, the member status string itself.
  expect(kvValue(checklist, "Member status")).toContain("active");
  expect(kvValue(checklist, "Member status")).toContain("allows an exit request");
  expect(kvValue(checklist, "Open exit request")).toBe("none");
  expect(kvValue(checklist, "Live guarantees (block until released/substituted)")).toBe("0");
  expect(kvValue(checklist, "Open loan applications (block until decided)")).toBe("0");
  expect(kvValue(checklist, "Unresolved write-off claim")).toBe("no");
  expect(kvValue(checklist, "Active loans (informational — net within the settlement)")).toBe("2");
  expect(kvValue(checklist, "Advisory verdict")).toBe("eligible to request");

  // The honest caveat: lock-free advisory facts; the server re-verdicts
  // under locks at request time and at settlement.
  expect(within(checklist).getByText(/read WITHOUT/)).toBeInTheDocument();
  expect(within(checklist).getByText(/re-verdicts every blocker under locks/)).toBeInTheDocument();
  expect(within(checklist).getByText(/AGAIN at settlement/)).toBeInTheDocument();

  // Least disclosure holds on screen: the checklist carries NO amount —
  // no KES-formatted figure and no decimal money string anywhere in it.
  expect(within(checklist).queryByText(/KES/)).toBeNull();
  expect(checklist.textContent).not.toMatch(/\d+\.\d{2}/);

  // The checklist precedes submission: with it on screen, the request
  // still goes through the full typed confirmation and fires ONCE.
  expect(mocked.createExitRequest).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Request exit…" }));
  await user.type(await screen.findByLabelText('Type "M-0001" to confirm'), "M-0001");
  // getByRole name matching is a FULL-string match by default, so this
  // is the danger confirm button, never the "Request exit…" submit.
  await user.click(screen.getByRole("button", { name: "Request exit" }));
  await waitFor(() => expect(mocked.createExitRequest).toHaveBeenCalledTimes(1));
});

test("self-gating: a loaded advisory BLOCKED verdict withholds the submit affordance and renders the server's rows verbatim; the ELIGIBLE default keeps it enabled (falsifiable both ways)", async () => {
  mocked.fetchExitEligibility.mockResolvedValue(BLOCKED);
  const user = userEvent.setup();
  mountDrawer();

  const checklist = await selectMember(user);

  // The rows are the server's, verbatim — distinct counts prove no
  // cross-wiring; the verdict is the SERVER's advisory_eligible field
  // (status_allows_exit stayed true — a client-side conjunction of
  // the rows is NOT what renders).
  expect(kvValue(checklist, "Open exit request")).toBe("one already exists — BLOCKS");
  expect(kvValue(checklist, "Live guarantees (block until released/substituted)")).toBe("3");
  expect(kvValue(checklist, "Open loan applications (block until decided)")).toBe("4");
  expect(kvValue(checklist, "Unresolved write-off claim")).toBe("yes — BLOCKS");
  expect(kvValue(checklist, "Member status")).toContain("dormant");
  expect(kvValue(checklist, "Advisory verdict")).toBe("blocked");

  // SELF-GATING: the affordance is structurally withheld…
  expect(screen.getByRole("button", { name: "Request exit…" })).toBeDisabled();
  // …with the honest server-enforces-regardless banner.
  expect(screen.getByText(/Advisory blockers are present/)).toBeInTheDocument();
  expect(mocked.createExitRequest).not.toHaveBeenCalled();
});

test("submit stays ENABLED on the eligible verdict (the gating leg's falsifiable counterpart)", async () => {
  const user = userEvent.setup();
  mountDrawer();
  await selectMember(user);
  expect(screen.getByRole("button", { name: "Request exit…" })).toBeEnabled();
  expect(screen.queryByText(/Advisory blockers are present/)).toBeNull();
});

test("advisory-only degradation: a FAILED eligibility read never gates — the honest note renders and the request path stays open (the server enforces under locks regardless)", async () => {
  mocked.fetchExitEligibility.mockRejectedValue(new Error("network down"));
  const user = userEvent.setup();
  mountDrawer();

  await screen.findByRole("option", { name: /Jane Wanjiku/ });
  await user.selectOptions(screen.getByLabelText("Member"), MEMBER_ID);

  // The honest degradation note (Providers retry the query ONCE with a
  // backoff before erroring — allow for the retry delay).
  expect(
    await screen.findByText(
      /advisory eligibility checklist could not be loaded/i,
      undefined,
      { timeout: 5000 },
    ),
  ).toBeInTheDocument();
  expect(screen.queryByTestId("eligibility-checklist")).toBeNull();

  // NOT gated: advisory only — the server verdict at request time is
  // the enforcer; the affordance stays open.
  expect(screen.getByRole("button", { name: "Request exit…" })).toBeEnabled();
  expect(screen.queryByText(/Advisory blockers are present/)).toBeNull();
});

test("XSS inertness: a hostile member_status renders as inert TEXT in the checklist — no markup materialises (defence in depth behind the reject-unknowns boundary)", async () => {
  mocked.fetchExitEligibility.mockResolvedValue({
    ...ELIGIBLE,
    member_status: HOSTILE_STATUS,
  } as unknown as ExitEligibility);
  const user = userEvent.setup();
  mountDrawer();

  const checklist = await selectMember(user);

  // The hostile string is VISIBLE as text…
  expect(kvValue(checklist, "Member status")).toContain(HOSTILE_STATUS);
  // …but inert: no element was created from it and no handler ran.
  expect(checklist.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});
