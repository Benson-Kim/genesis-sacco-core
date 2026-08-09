/**
 * Branch roster panels (#31 ledger (j).1) THROUGH THE REAL DRAWER
 * CODE: BranchDetailDrawer mounted under the real <Providers> with
 * the feature API layer stubbed at the module boundary (the batch-4
 * reference-suite pattern).
 *
 * The batch's falsifiable claims:
 * - The batch-4 assignment permission split MIRRORED on reads: the
 *   USER roster mounts only with access_control:view, the MEMBER
 *   roster only with members:view — and the STRUCTURAL withholding
 *   legs prove settings-only grants mount NEITHER panel and NEITHER
 *   roster endpoint is probed (deny-by-default, gate 1.6).
 * - Keyset paging only: Load more follows the server cursor VERBATIM.
 * - Identity facts render VERBATIM as inert React text (names are
 *   operator free text — the module's named XSS threat); a dormant
 *   member's status renders honestly.
 * - A failed roster read renders the shared honest error state — no
 *   invented rows.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { BranchDetailDrawer } from "../components/BranchDetailDrawer";
import type { BranchMemberRosterRow, BranchUserRosterRow } from "../schemas";
import * as branchesApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchBranch: jest.fn(),
    fetchBranchUsersRosterPage: jest.fn(),
    fetchBranchMembersRosterPage: jest.fn(),
    assignUserToBranch: jest.fn(),
    assignMemberToBranch: jest.fn(),
    updateBranch: jest.fn(),
  };
});

jest.mock("@/modules/users/api", () => ({
  fetchUsersPage: jest.fn(),
  fetchUser: jest.fn(),
}));

jest.mock("@/modules/members/api", () => ({
  fetchMembersPage: jest.fn(),
  fetchMember: jest.fn(),
}));

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(branchesApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";

const HOSTILE_NAME = "<img src=x onerror=window.__pwned=5>Hostile Member";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const BRANCH = {
  id: BRANCH_ID,
  name: "Nairobi West",
  version: 1,
  created_at: "2026-07-01T08:00:00+00:00",
};

const USER_ROWS: BranchUserRosterRow[] = [
  {
    id: "aaaaaaaa-1111-2222-3333-444444444444",
    full_name: "Ann Achieng",
    email: "ann@sacco.example",
    status: "active",
  },
  {
    id: "aaaaaaaa-5555-6666-7777-888888888888",
    full_name: "Ben Baraka",
    email: "ben@sacco.example",
    status: "suspended",
  },
];

const MEMBER_ROWS: BranchMemberRosterRow[] = [
  {
    id: "cccccccc-1111-2222-3333-444444444444",
    member_no: "GP-7001",
    name: "Alpha Member",
    status: "active",
  },
  {
    id: "cccccccc-5555-6666-7777-888888888888",
    member_no: "GP-7002",
    name: "Bravo Member",
    status: "dormant",
  },
];

/** Both view grants — both rosters mount (no edit grants, so the
 * batch-4 assign panels stay out of this suite's way). */
const BOTH_VIEW_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
    {
      module: "access_control",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** FULL settings rights, NO people-module view: the structural
 * withholding leg — settings alone must disclose NO people records. */
const SETTINGS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: false },
  ],
};

/** access_control:view only — the user roster leg of the split. */
const ACCESS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
    {
      module: "access_control",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

/** members:view only — the member roster leg of the split. */
const MEMBERS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
    { module: "members", can_view: true, can_create: false, can_edit: false, can_approve: false },
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
      <BranchDetailDrawer branchId={BRANCH_ID} onClose={jest.fn()} />
    </Providers>,
  );
}

/** The panel is the subhead's parent block — scope queries inside it. */
async function findPanel(title: string): Promise<HTMLElement> {
  const head = await screen.findByText(title);
  return head.parentElement as HTMLElement;
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(BOTH_VIEW_PERMS);
  mocked.fetchBranch.mockResolvedValue(BRANCH);
  mocked.fetchBranchUsersRosterPage.mockResolvedValue({ items: USER_ROWS, nextCursor: null });
  mocked.fetchBranchMembersRosterPage.mockResolvedValue({
    items: MEMBER_ROWS,
    nextCursor: null,
  });
});

afterEach(() => {
  clearSession();
});

test("both rosters render their SERVER rows verbatim under their view grants — identity facts only, statuses honest (incl. Dormant)", async () => {
  mountDrawer();

  const users = await findPanel("Users assigned to this branch");
  expect(await within(users).findByText("Ann Achieng")).toBeInTheDocument();
  expect(within(users).getByText("ann@sacco.example")).toBeInTheDocument();
  expect(within(users).getByText("Active")).toBeInTheDocument();
  expect(within(users).getByText("Ben Baraka")).toBeInTheDocument();
  expect(within(users).getByText("Suspended")).toBeInTheDocument();

  const members = await findPanel("Members assigned to this branch");
  expect(await within(members).findByText("Alpha Member")).toBeInTheDocument();
  expect(within(members).getByText("GP-7001")).toBeInTheDocument();
  expect(within(members).getByText("Bravo Member")).toBeInTheDocument();
  // The dormancy state renders honestly, verbatim from the server.
  expect(within(members).getByText("Dormant")).toBeInTheDocument();

  expect(mocked.fetchBranchUsersRosterPage).toHaveBeenCalledWith(BRANCH_ID, null);
  expect(mocked.fetchBranchMembersRosterPage).toHaveBeenCalledWith(BRANCH_ID, null);
});

test("STRUCTURAL withholding: FULL settings rights alone mount NEITHER roster panel and NEITHER roster endpoint is probed (the batch-4 split mirrored on reads)", async () => {
  grantPermissions(SETTINGS_ONLY_PERMS);
  mountDrawer();

  // The drawer itself renders for the settings operator…
  expect(await screen.findByRole("dialog", { name: "Branch" })).toBeInTheDocument();
  await screen.findByText("Nairobi West");
  // …but no roster surface exists and no roster read ever fired.
  expect(screen.queryByText("Users assigned to this branch")).toBeNull();
  expect(screen.queryByText("Members assigned to this branch")).toBeNull();
  await waitFor(() => {
    expect(mocked.fetchBranchUsersRosterPage).not.toHaveBeenCalled();
    expect(mocked.fetchBranchMembersRosterPage).not.toHaveBeenCalled();
  });
});

test("the split's user leg: access_control:view alone mounts ONLY the user roster; the member roster is never probed", async () => {
  grantPermissions(ACCESS_ONLY_PERMS);
  mountDrawer();

  await findPanel("Users assigned to this branch");
  expect(screen.queryByText("Members assigned to this branch")).toBeNull();
  await waitFor(() => expect(mocked.fetchBranchUsersRosterPage).toHaveBeenCalled());
  expect(mocked.fetchBranchMembersRosterPage).not.toHaveBeenCalled();
});

test("the split's member leg: members:view alone mounts ONLY the member roster; the user roster is never probed", async () => {
  grantPermissions(MEMBERS_ONLY_PERMS);
  mountDrawer();

  await findPanel("Members assigned to this branch");
  expect(screen.queryByText("Users assigned to this branch")).toBeNull();
  await waitFor(() => expect(mocked.fetchBranchMembersRosterPage).toHaveBeenCalled());
  expect(mocked.fetchBranchUsersRosterPage).not.toHaveBeenCalled();
});

test("keyset paging: Load more follows the server roster cursor VERBATIM (gate 1.3)", async () => {
  const user = userEvent.setup();
  mocked.fetchBranchUsersRosterPage
    .mockResolvedValueOnce({ items: USER_ROWS, nextCursor: "opaque-roster-cursor-§1" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountDrawer();

  const users = await findPanel("Users assigned to this branch");
  await within(users).findByText("Ann Achieng");
  await user.click(within(users).getByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchBranchUsersRosterPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchBranchUsersRosterPage.mock.calls[0]?.[1]).toBeNull();
  expect(mocked.fetchBranchUsersRosterPage.mock.calls[1]?.[1]).toBe("opaque-roster-cursor-§1");
});

test("hostile roster NAME (operator free text — the named XSS threat) renders as inert TEXT; no markup materialises", async () => {
  mocked.fetchBranchMembersRosterPage.mockResolvedValue({
    items: [{ ...MEMBER_ROWS[0]!, name: HOSTILE_NAME }],
    nextCursor: null,
  });
  const { container } = mountDrawer();

  expect(await screen.findByText(HOSTILE_NAME)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("a failed roster read renders the shared honest error state — no invented rows", async () => {
  mocked.fetchBranchUsersRosterPage.mockRejectedValue(new Error("boom"));
  mountDrawer();

  const users = await findPanel("Users assigned to this branch");
  // The Providers default retries queries ONCE (~1s backoff) before
  // surfacing the error state — allow for it (the house pattern).
  expect(
    await within(users).findByText(/Could not load this list/, undefined, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(within(users).queryByText("Ann Achieng")).toBeNull();
});
