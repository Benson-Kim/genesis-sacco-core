/**
 * Member branch attribution (#31 ledger (j).2) THROUGH THE REAL
 * DRAWER CODE: the Branch panel of MemberKycDrawer mounted under the
 * real <Providers> with the feature API layers stubbed at their
 * module boundaries (the members reference-suite pattern).
 *
 * The batch's falsifiable claims:
 * - NULL renders the honest UNASSIGNED affordance — attribution is
 *   never invented (FM-B class).
 * - The branch NAME resolves ONLY under settings:view (the contract's
 *   own GET /branches/{id} read); without the grant the SHORT id
 *   renders with the full UUID on title (the created_by convention)
 *   and NO branch read is ever probed — least disclosure structural,
 *   not cosmetic.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { fetchBranch } from "@/modules/branches/api";
import { MemberKycDrawer } from "../components/MemberKycDrawer";
import type { Member } from "../schemas";
import * as membersApi from "../api";
import * as kycApi from "../kycApi";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchMemberDetail: jest.fn(),
  };
});

jest.mock("../kycApi", () => {
  const actual = jest.requireActual<typeof import("../kycApi")>("../kycApi");
  return {
    ...actual,
    fetchKycProfile: jest.fn(),
    fetchKycDocumentsPage: jest.fn(),
  };
});

jest.mock("@/modules/branches/api", () => ({
  fetchBranch: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mockedMembers = jest.mocked(membersApi);
const mockedKyc = jest.mocked(kycApi);
const mockedFetchBranch = jest.mocked(fetchBranch);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const MEMBER_ID = "11111111-1111-1111-1111-111111111111";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const MEMBER: Member = {
  id: MEMBER_ID,
  member_no: "GP-0001",
  type: "person",
  name: "Jane Wanjiku",
  phone: null,
  email: null,
  status: "active",
  version: 1,
  branch_id: null,
  dividend_payout: null,
};

const AGGREGATES = {
  deposits_total: "1000.11",
  shares_total: "200.22",
  loans_outstanding: "300.33",
  guarantees_pledged: "40.04",
};

/** members grants only — NO settings:view (the least-disclosure leg). */
const MEMBERS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** members + settings view — the resolve leg. */
const WITH_SETTINGS_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

function grantPermissions(perms: unknown) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountDrawer() {
  return render(
    <Providers>
      <MemberKycDrawer member={MEMBER} onStartWizard={jest.fn()} onClose={jest.fn()} />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(MEMBERS_ONLY_PERMS);
  mockedMembers.fetchMemberDetail.mockResolvedValue({ ...MEMBER, aggregates: AGGREGATES });
  // "No profile yet" is the honest 404 state — not under test here.
  mockedKyc.fetchKycProfile.mockRejectedValue(new ApiError(404, "not_found", "corr-kyc"));
  mockedKyc.fetchKycDocumentsPage.mockResolvedValue({ items: [], nextCursor: null });
});

afterEach(() => {
  clearSession();
});

test("NULL branch_id renders the honest UNASSIGNED affordance — attribution is never invented, and no branch read fires", async () => {
  mountDrawer();

  expect(await screen.findByText("Home branch")).toBeInTheDocument();
  expect(
    await screen.findByText("— (unassigned; branches console assigns)"),
  ).toBeInTheDocument();
  await waitFor(() => expect(mockedFetchBranch).not.toHaveBeenCalled());
});

test("an assigned branch WITHOUT settings:view renders the SHORT id (full UUID on title) and NEVER probes the branch read — least disclosure is structural", async () => {
  mockedMembers.fetchMemberDetail.mockResolvedValue({
    ...MEMBER,
    branch_id: BRANCH_ID,
    aggregates: AGGREGATES,
  });
  mountDrawer();

  const shortId = await screen.findByText(BRANCH_ID.slice(0, 8));
  expect(shortId).toHaveAttribute("title", BRANCH_ID);
  await waitFor(() => expect(mockedFetchBranch).not.toHaveBeenCalled());
});

test("an assigned branch WITH settings:view resolves the branch NAME through the contract's own read", async () => {
  grantPermissions(WITH_SETTINGS_PERMS);
  mockedMembers.fetchMemberDetail.mockResolvedValue({
    ...MEMBER,
    branch_id: BRANCH_ID,
    aggregates: AGGREGATES,
  });
  mockedFetchBranch.mockResolvedValue({
    id: BRANCH_ID,
    name: "Nairobi West",
    version: 1,
    created_at: "2026-07-01T08:00:00+00:00",
  });
  mountDrawer();

  expect(await screen.findByText("Nairobi West")).toBeInTheDocument();
  expect(mockedFetchBranch).toHaveBeenCalledWith(BRANCH_ID);
  expect(mockedFetchBranch).toHaveBeenCalledTimes(1);
});
