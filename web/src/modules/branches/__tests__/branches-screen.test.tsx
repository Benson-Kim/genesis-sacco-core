/**
 * Branches register + registry-write adversarial tests THROUGH THE
 * REAL SCREEN CODE (issue #31 batch 4 — the P13.6 registry console).
 * The feature API layer and the cross-module user/member reads are
 * stubbed at their module boundaries; rendering, keyset paging,
 * permission gating, idempotency-key custody, the fresh reads, 409
 * handling and the structural withholding are the real shipped code,
 * mounted under the REAL <Providers>.
 *
 * Centrepieces:
 * - Branch NAMES are operator free text — the named XSS threat: a
 *   hostile name renders as inert TEXT, no markup materialises.
 * - Deny-by-default per the ROUTER's permission split: settings:view
 *   alone gets NO create/backfill/rename affordance; the assignment
 *   panels mount ONLY with access_control:edit (users) / members:edit
 *   (members) — settings rights alone never expose people edits.
 * - CREATE and RENAME each have a double-submit single-write proof;
 *   RENAME pins the FRESH record version; 409 → EXACTLY ONE attempt +
 *   the shared ConflictBanner; the explicit reload NEVER replays and
 *   the re-entered intent carries a ROTATED Idempotency-Key (!60 F3).
 * - ASSIGNMENT structurally refuses to fire without the loaded fresh
 *   USER/MEMBER record (the F-M1 class) and folds its version into
 *   the key material.
 * - The BACKFILL re-run is a NEW intent (rotated key, per-success
 *   counter) and the all-zero no-op report renders verbatim.
 * - NO per-tab registry exists in this module (no maker-checker
 *   contract on P13.6), so there is no teardown surface — and no
 *   fresh copy of the S3 primitive.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { fetchUser, fetchUsersPage } from "@/modules/users/api";
import { fetchMember, fetchMembersPage } from "@/modules/members/api";
import { BranchesScreen } from "../components/BranchesScreen";
import { branchSchema, type BranchRecord } from "../schemas";
import * as branchesApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchBranchesPage: jest.fn(),
    fetchBranch: jest.fn(),
    fetchBranchUsersRosterPage: jest.fn(),
    fetchBranchMembersRosterPage: jest.fn(),
    createBranch: jest.fn(),
    updateBranch: jest.fn(),
    assignUserToBranch: jest.fn(),
    assignMemberToBranch: jest.fn(),
    runBranchBackfill: jest.fn(),
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
const mockedFetchUsersPage = jest.mocked(fetchUsersPage);
const mockedFetchUser = jest.mocked(fetchUser);
const mockedFetchMembersPage = jest.mocked(fetchMembersPage);
const mockedFetchMember = jest.mocked(fetchMember);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const BRANCH_ID = "bbbbbbbb-aaaa-bbbb-cccc-111111111111";
const TARGET_USER_ID = "aaaaaaaa-1111-2222-3333-444444444444";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(claims: object): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ ...claims, exp })}.sig`;
}

function branch(overrides: Partial<BranchRecord> = {}): BranchRecord {
  return {
    id: BRANCH_ID,
    name: "Nairobi West",
    version: 1,
    created_at: "2026-08-01T09:00:00+00:00",
    ...overrides,
  };
}

const officerUser = {
  id: TARGET_USER_ID,
  full_name: "Asha Officer",
  email: "asha@sacco.co.ke",
  phone: null,
  branch: null,
  role_id: "role-1",
  role_name: "Loan Officer",
  status: "active",
  last_active_at: null,
  version: 3,
};

const memberRow = {
  id: "cccccccc-1111-2222-3333-444444444444",
  member_no: "M-0007",
  type: "person" as const,
  name: "Wanjiku Member",
  phone: null,
  email: null,
  status: "active" as const,
  version: 6,
  branch_id: null,
  dividend_payout: null,
};

/** Every grant this console can use (register + rename + both panels). */
const FULL_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: false },
    {
      module: "access_control",
      can_view: true,
      can_create: false,
      can_edit: true,
      can_approve: false,
    },
    { module: "members", can_view: true, can_create: false, can_edit: true, can_approve: false },
  ],
};

/** settings:view ONLY — the register is readable, every write withheld. */
const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: false, can_edit: false, can_approve: false },
  ],
};

/** Full settings rights but NO people-module edit: the router's
 * registry/entity split — rename mounts, both assign panels withheld. */
const SETTINGS_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "settings", can_view: true, can_create: true, can_edit: true, can_approve: false },
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
      <BranchesScreen />
    </Providers>,
  );
}

/** Open the detail drawer by clicking the (only) register row. */
async function openDetail(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Nairobi West"));
  return await screen.findByRole("dialog", { name: "Branch" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt({ sub: ADMIN_ID }), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchBranchesPage.mockResolvedValue({ items: [branch()], nextCursor: null });
  mocked.fetchBranch.mockResolvedValue(branch());
  // Roster reads (#31 (j).1): empty defaults so suites not about
  // rosters never hit the real network; the roster suite
  // (branches-rosters-screen.test.tsx) owns the assertions.
  mocked.fetchBranchUsersRosterPage.mockResolvedValue({ items: [], nextCursor: null });
  mocked.fetchBranchMembersRosterPage.mockResolvedValue({ items: [], nextCursor: null });
  mocked.createBranch.mockResolvedValue(branch());
  mocked.updateBranch.mockResolvedValue(branch({ name: "Nairobi West HQ", version: 2 }));
  mocked.assignUserToBranch.mockResolvedValue({
    entity_id: TARGET_USER_ID,
    branch_id: BRANCH_ID,
    branch_name: "Nairobi West",
    version: 4,
  });
  mocked.assignMemberToBranch.mockResolvedValue({
    entity_id: memberRow.id,
    branch_id: BRANCH_ID,
    branch_name: "Nairobi West",
    version: 7,
  });
  mocked.runBranchBackfill.mockResolvedValue({
    scanned: 12,
    branches_created: 3,
    users_linked: 12,
    batches: 1,
  });
  mockedFetchUsersPage.mockResolvedValue({ items: [officerUser], nextCursor: null });
  mockedFetchUser.mockResolvedValue(officerUser);
  mockedFetchMembersPage.mockResolvedValue({ items: [memberRow], nextCursor: null });
  mockedFetchMember.mockResolvedValue(memberRow);
});

afterEach(() => {
  clearSession();
});

test("register renders the registry rows VERBATIM (name, stamp, short record id) — and the registry/entity permission split is stated", async () => {
  mountScreen();

  expect(await screen.findByText("Nairobi West")).toBeInTheDocument();
  expect(screen.getByTitle(BRANCH_ID)).toHaveTextContent(BRANCH_ID.slice(0, 8));
  expect(screen.getByText(/never a settings right/)).toBeInTheDocument();
});

test("empty state and error state render honestly — a rejected page can never render as registry rows", async () => {
  mocked.fetchBranchesPage.mockResolvedValue({ items: [], nextCursor: null });
  const { unmount } = mountScreen();
  expect(await screen.findByText("No branches registered yet.")).toBeInTheDocument();
  unmount();

  // Hostile/garbage shapes are rejected by the REAL (unmocked) boundary…
  expect(branchSchema.safeParse({ ...branch(), created_at: "garbage" }).success).toBe(false);
  expect(branchSchema.safeParse({ ...branch(), version: "1" }).success).toBe(false);
  // …so such a page reaches the screen only as a rejected fetch.
  mocked.fetchBranchesPage.mockRejectedValue(new Error("contract violation at the boundary"));
  mountScreen();
  expect(
    await screen.findByText(/Could not load this list/, undefined, { timeout: 5000 }),
  ).toBeInTheDocument();
  expect(screen.queryByText("Nairobi West")).toBeNull();
});

test("keyset paging: Load more follows the server cursor VERBATIM (gate 1.3); no filter affordance exists (the contract declares none)", async () => {
  const user = userEvent.setup();
  mocked.fetchBranchesPage
    .mockResolvedValueOnce({ items: [branch()], nextCursor: "opaque-cursor-§1" })
    .mockResolvedValue({ items: [], nextCursor: null });
  mountScreen();

  await user.click(await screen.findByRole("button", { name: "Load more" }));
  await waitFor(() => expect(mocked.fetchBranchesPage).toHaveBeenCalledTimes(2));
  expect(mocked.fetchBranchesPage.mock.calls[0]?.[0]).toBeNull();
  expect(mocked.fetchBranchesPage.mock.calls[1]?.[0]).toBe("opaque-cursor-§1");
  expect(screen.queryByLabelText("Status")).toBeNull();
});

test("hostile branch NAME (operator free text — the named XSS threat) renders as inert TEXT everywhere; no markup materialises", async () => {
  const HOSTILE = '<img src=x onerror=window.__pwned=1>';
  mocked.fetchBranchesPage.mockResolvedValue({
    items: [branch({ name: HOSTILE })],
    nextCursor: null,
  });
  const { container } = mountScreen();

  expect(await screen.findByText(HOSTILE)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});

test("deny-by-default (settings:view only): NO create, NO backfill, NO rename, NO assignment panel — the drawer states the split honestly; zero writes possible", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  await screen.findByText("Nairobi West");
  expect(screen.queryByRole("button", { name: "+ Register branch" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Run legacy backfill…" })).toBeNull();

  const drawer = await openDetail(user);
  expect(within(drawer).getByText(/You hold no edit grant for this record/)).toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: "Rename branch" })).toBeNull();
  expect(within(drawer).queryByText("Assign a user to this branch")).toBeNull();
  expect(within(drawer).queryByText("Assign a member to this branch")).toBeNull();
  expect(mocked.createBranch).not.toHaveBeenCalled();
  expect(mocked.updateBranch).not.toHaveBeenCalled();
  expect(mocked.assignUserToBranch).not.toHaveBeenCalled();
  expect(mocked.assignMemberToBranch).not.toHaveBeenCalled();
  expect(mocked.runBranchBackfill).not.toHaveBeenCalled();
});

test("the registry/entity split (the router's decision): FULL settings rights alone mount the rename but NEITHER assignment panel — and neither people list is even probed", async () => {
  grantPermissions(SETTINGS_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  const drawer = await openDetail(user);
  expect(await within(drawer).findByRole("button", { name: "Rename branch" })).toBeInTheDocument();
  expect(within(drawer).queryByText("Assign a user to this branch")).toBeNull();
  expect(within(drawer).queryByText("Assign a member to this branch")).toBeNull();
  expect(mockedFetchUsersPage).not.toHaveBeenCalled();
  expect(mockedFetchMembersPage).not.toHaveBeenCalled();
});

test("CREATE: a triple-clicked submission produces exactly ONE write with the TRIMMED name; the result panel renders the SERVER record verbatim (SPENT affordance)", async () => {
  const user = userEvent.setup();
  let releaseCreate: (value: BranchRecord) => void = () => undefined;
  mocked.createBranch.mockImplementation(
    () =>
      new Promise<BranchRecord>((resolve) => {
        releaseCreate = resolve;
      }),
  );
  mountScreen();
  await screen.findByText("Nairobi West");

  await user.click(screen.getByRole("button", { name: "+ Register branch" }));
  const drawer = await screen.findByRole("dialog", { name: "Register branch" });
  await user.type(within(drawer).getByLabelText("Branch name"), "  Kisumu Central  ");
  const submit = within(drawer).getByRole("button", { name: "Register branch" });
  await user.click(submit);
  await user.click(submit);
  await user.click(submit);
  expect(mocked.createBranch).toHaveBeenCalledTimes(1);
  expect(mocked.createBranch.mock.calls[0]?.[0]).toBe("Kisumu Central");

  releaseCreate(branch({ id: "new-branch", name: "Kisumu Central" }));

  expect(await screen.findByText("Branch registered")).toBeInTheDocument();
  const panel = screen.getByRole("status");
  expect(within(panel).getByText("Kisumu Central")).toBeInTheDocument();
  // SPENT: the form is replaced by the result panel.
  expect(within(drawer).queryByRole("button", { name: "Register branch" })).toBeNull();
  expect(mocked.createBranch).toHaveBeenCalledTimes(1);
});

test("CREATE client validation: an empty name never reaches the wire — the labelled field error renders (a11y wiring)", async () => {
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText("Nairobi West");

  await user.click(screen.getByRole("button", { name: "+ Register branch" }));
  const drawer = await screen.findByRole("dialog", { name: "Register branch" });
  await user.click(within(drawer).getByRole("button", { name: "Register branch" }));

  expect(await within(drawer).findByText("Enter a branch name.")).toBeInTheDocument();
  expect(within(drawer).getByLabelText("Branch name")).toHaveAttribute("aria-invalid", "true");
  expect(mocked.createBranch).not.toHaveBeenCalled();
});

test("CREATE 409 (duplicate name): EXACTLY ONE attempt + ConflictBanner; the explicit reload refetches the register and NEVER replays; the re-entered intent carries a ROTATED key (!60 F3)", async () => {
  const user = userEvent.setup();
  mocked.createBranch.mockRejectedValue(new ApiError(409, "conflict", "corr-dup"));
  mountScreen();
  await screen.findByText("Nairobi West");

  await user.click(screen.getByRole("button", { name: "+ Register branch" }));
  const drawer = await screen.findByRole("dialog", { name: "Register branch" });
  await user.type(within(drawer).getByLabelText("Branch name"), "Nairobi West");
  await user.click(within(drawer).getByRole("button", { name: "Register branch" }));

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(mocked.createBranch).toHaveBeenCalledTimes(1);
  const staleKey = mocked.createBranch.mock.calls[0]?.[1];
  expect(staleKey).toBeTruthy();

  const listCallsBefore = mocked.fetchBranchesPage.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/the conflicted registration was withdrawn, nothing was replayed/),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(mocked.fetchBranchesPage.mock.calls.length).toBeGreaterThan(listCallsBefore),
  );
  expect(mocked.createBranch).toHaveBeenCalledTimes(1);

  // Re-entering the byte-identical name after the acknowledged
  // conflict is a NEW intent: the key ROTATES.
  mocked.createBranch.mockResolvedValue(branch());
  await user.click(within(drawer).getByRole("button", { name: "Register branch" }));
  await waitFor(() => expect(mocked.createBranch).toHaveBeenCalledTimes(2));
  const freshKey = mocked.createBranch.mock.calls[1]?.[1];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("RENAME pins the FRESH record version; a double-clicked submit produces exactly ONE write; 409 → ONE attempt + ConflictBanner; the reload refetches the record and the re-entry carries a ROTATED key", async () => {
  const user = userEvent.setup();
  // HELD promise: the double-click proof must observe the pending
  // short-circuit, then the held attempt is rejected with the 409.
  let rejectRename: (error: unknown) => void = () => undefined;
  mocked.updateBranch.mockImplementation(
    () =>
      new Promise<BranchRecord>((_resolve, reject) => {
        rejectRename = reject;
      }),
  );
  mountScreen();

  const drawer = await openDetail(user);
  // The fresh read (record class) backs the pinned version.
  await waitFor(() => expect(mocked.fetchBranch).toHaveBeenCalledWith(BRANCH_ID));

  const input = await within(drawer).findByLabelText("Branch name");
  await user.clear(input);
  await user.type(input, "Nairobi West HQ");
  const renameButton = within(drawer).getByRole("button", { name: "Rename branch" });
  await user.click(renameButton);
  await user.click(renameButton);

  expect(mocked.updateBranch).toHaveBeenCalledTimes(1);
  rejectRename(new ApiError(409, "conflict", "corr-stale"));
  // (branchId, version, name, key): the PINNED fresh version travels.
  expect(mocked.updateBranch.mock.calls[0]?.[0]).toBe(BRANCH_ID);
  expect(mocked.updateBranch.mock.calls[0]?.[1]).toBe(1);
  expect(mocked.updateBranch.mock.calls[0]?.[2]).toBe("Nairobi West HQ");
  const staleKey = mocked.updateBranch.mock.calls[0]?.[3];

  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();

  // Explicit reload: the record refetches (advancing the version), the
  // stale submission is NEVER replayed.
  mocked.fetchBranch.mockResolvedValue(branch({ version: 2 }));
  const detailReadsBefore = mocked.fetchBranch.mock.calls.length;
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  await waitFor(() => expect(mocked.fetchBranch.mock.calls.length).toBeGreaterThan(detailReadsBefore));
  expect(mocked.updateBranch).toHaveBeenCalledTimes(1);

  // The re-entered intent pins the RELOADED version and ROTATES the key.
  mocked.updateBranch.mockResolvedValue(branch({ name: "Nairobi West HQ", version: 3 }));
  const freshInput = await within(drawer).findByLabelText("Branch name");
  await user.clear(freshInput);
  await user.type(freshInput, "Nairobi West HQ");
  await user.click(within(drawer).getByRole("button", { name: "Rename branch" }));
  await waitFor(() => expect(mocked.updateBranch).toHaveBeenCalledTimes(2));
  expect(mocked.updateBranch.mock.calls[1]?.[1]).toBe(2);
  const freshKey = mocked.updateBranch.mock.calls[1]?.[3];
  expect(freshKey).toBeTruthy();
  expect(freshKey).not.toBe(staleKey);
});

test("ASSIGN USER: structurally refuses to fire before the FRESH user read lands; then exactly ONE write pinning the fresh user version; the result renders the SERVER's assignment fact verbatim", async () => {
  const user = userEvent.setup();
  // Hold the fresh read so the structural withholding is observable.
  let releaseUser: (value: typeof officerUser) => void = () => undefined;
  mockedFetchUser.mockImplementation(
    () =>
      new Promise<typeof officerUser>((resolve) => {
        releaseUser = resolve;
      }) as ReturnType<typeof fetchUser>,
  );
  mountScreen();

  const drawer = await openDetail(user);
  const panelTitle = await within(drawer).findByText("Assign a user to this branch");
  expect(panelTitle).toBeInTheDocument();

  await user.selectOptions(
    within(drawer).getByLabelText("User"),
    await within(drawer).findByRole("option", { name: "Asha Officer · asha@sacco.co.ke" }),
  );

  // The fresh read is in flight: the affordance is STRUCTURALLY
  // withheld (disabled) — no click can produce a write.
  const assignButton = within(drawer).getByRole("button", { name: "Assign user to Nairobi West" });
  expect(assignButton).toBeDisabled();
  expect(mocked.assignUserToBranch).not.toHaveBeenCalled();

  releaseUser(officerUser);
  await within(drawer).findByText(/record version 3.*read fresh/);

  await user.click(assignButton);
  await waitFor(() => expect(mocked.assignUserToBranch).toHaveBeenCalledTimes(1));
  // (branchId, userId, version, key): the FRESH user version travels.
  expect(mocked.assignUserToBranch.mock.calls[0]?.[0]).toBe(BRANCH_ID);
  expect(mocked.assignUserToBranch.mock.calls[0]?.[1]).toBe(TARGET_USER_ID);
  expect(mocked.assignUserToBranch.mock.calls[0]?.[2]).toBe(3);

  expect(await within(drawer).findByText("Assignment recorded")).toBeInTheDocument();
  expect(within(drawer).getByText("New record version")).toBeInTheDocument();
});

test("BACKFILL: exactly ONE write per click with the server-default batch size; the report renders verbatim; the RE-RUN is a NEW intent (ROTATED key) and the all-zero no-op report renders honestly", async () => {
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText("Nairobi West");

  await user.click(screen.getByRole("button", { name: "Run legacy backfill…" }));
  const dialog = await screen.findByRole("dialog", { name: "Run legacy branch backfill" });

  await user.click(within(dialog).getByRole("button", { name: "Run backfill" }));
  await within(dialog).findByText("Backfill run report");
  expect(mocked.runBranchBackfill).toHaveBeenCalledTimes(1);
  const firstKey = mocked.runBranchBackfill.mock.calls[0]?.[0];
  expect(within(dialog).getByText("Branches created")).toBeInTheDocument();

  // The idempotent RE-RUN: a NEW intent with a ROTATED key; the
  // lock-free no-op is PROVEN by the zero counts, rendered verbatim.
  mocked.runBranchBackfill.mockResolvedValue({
    scanned: 0,
    branches_created: 0,
    users_linked: 0,
    batches: 0,
  });
  await user.click(within(dialog).getByRole("button", { name: "Re-run backfill" }));
  await waitFor(() => expect(mocked.runBranchBackfill).toHaveBeenCalledTimes(2));
  const secondKey = mocked.runBranchBackfill.mock.calls[1]?.[0];
  expect(secondKey).toBeTruthy();
  expect(secondKey).not.toBe(firstKey);
  expect((await within(dialog).findAllByText("0")).length).toBeGreaterThanOrEqual(4);
});
