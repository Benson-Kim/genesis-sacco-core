/**
 * Dormancy worklist + operations run (#31 ledger (f)) THROUGH THE
 * REAL SCREEN CODE, mounted under the real <Providers> with the
 * feature API layer stubbed at the module boundary (the members
 * reference-suite pattern).
 *
 * The batch's falsifiable claims:
 * - DISCOVERY HONESTY: the worklist is the register's own
 *   status=dormant filter — the fetch carries the PINNED filter and
 *   the screen offers NO filter affordance (no contract change was
 *   needed and none exists to consume).
 * - The run affordance mounts ONLY with members:edit (pure UX; the
 *   server enforces regardless) — view-only roles cannot even open
 *   the dialog and the job is never called.
 * - Typed confirmation gates the run; a double-click produces EXACTLY
 *   ONE write; the SPENT affordance replaces the action with the
 *   SERVER's verbatim counts; an intentional re-run rotates the
 *   Idempotency-Key (MATERIAL rule 4).
 * - A 409 (period unconfigured — the run fails CLOSED) is ONE
 *   attempt, rendered honestly, never replayed; the acknowledged
 *   conflict rotates the key (T3).
 * - Hostile member names render as inert TEXT (named XSS threat).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { DormancyWorklistScreen } from "../components/DormancyWorklistScreen";
import type { DormancyRun, Member } from "../schemas";
import * as membersApi from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchMembersPage: jest.fn(),
    runDormancyJob: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(membersApi);
const mockedPermissions = jest.mocked(usePermissions);

const ADMIN_ID = "99999999-9999-9999-9999-999999999999";

const HOSTILE_NAME = "<img src=x onerror=window.__pwned=3>Dormant Member";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const DORMANT_MEMBER: Member = {
  id: "11111111-1111-1111-1111-111111111111",
  member_no: "GP-0031",
  type: "person",
  name: "Grace Njeri",
  phone: "+254700000031",
  email: null,
  status: "dormant",
  version: 4,
  branch_id: null,
  dividend_payout: null,
};

const RUN_REPORT: DormancyRun = {
  as_of: "2026-08-06",
  cutoff: "2025-08-06",
  period_months: 12,
  scanned: 240,
  transitioned: 7,
  batches: 2,
};

const EDIT_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
    { module: "members", can_view: true, can_create: false, can_edit: true, can_approve: false },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ADMIN_ID,
  permissions: [
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

function mountScreen() {
  return render(
    <Providers>
      <DormancyWorklistScreen />
    </Providers>,
  );
}

/** Open the dialog and drive the typed confirmation to the armed
 * confirm button. */
async function armRun(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Run dormancy scan…" }));
  const dialog = await screen.findByRole("dialog", { name: "Run dormancy scan" });
  await user.click(within(dialog).getByRole("button", { name: "Run dormancy scan…" }));
  const confirm = await screen.findByRole("dialog", { name: "Confirm dormancy scan" });
  await user.type(
    within(confirm).getByLabelText('Type "mark dormant" to confirm'),
    "mark dormant",
  );
  return within(confirm).getByRole("button", { name: "Run dormancy scan" });
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(EDIT_PERMS);
  mocked.fetchMembersPage.mockResolvedValue({ items: [DORMANT_MEMBER], nextCursor: null });
  mocked.runDormancyJob.mockResolvedValue(RUN_REPORT);
});

afterEach(() => {
  clearSession();
});

test("the worklist is the register's PINNED dormant filter — the fetch carries status=dormant and NO filter affordance exists (discovery honesty: no contract change)", async () => {
  mountScreen();

  expect(await screen.findByText("Grace Njeri")).toBeInTheDocument();
  expect(screen.getByText("GP-0031")).toBeInTheDocument();
  expect(screen.getByText("Dormant")).toBeInTheDocument();
  expect(mocked.fetchMembersPage).toHaveBeenCalledWith(
    { status: "dormant", type: "" },
    null,
  );
  // No segmented filter, no status choice: the worklist is pinned.
  expect(screen.queryByRole("group")).toBeNull();
  expect(screen.queryByRole("button", { name: "All" })).toBeNull();
});

test("deny-by-default: members:view alone gets NO run affordance and the job is never called (pure UX; server enforces regardless)", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  mountScreen();

  expect(await screen.findByText("Grace Njeri")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Run dormancy scan…" })).toBeNull();
  expect(mocked.runDormancyJob).not.toHaveBeenCalled();
});

test("typed confirmation gates the run; a DOUBLE-click fires EXACTLY ONE write; the SPENT affordance renders the SERVER's counts verbatim; a re-run is a NEW intent (rotated key)", async () => {
  const user = userEvent.setup();
  let release: (value: DormancyRun) => void = () => undefined;
  mocked.runDormancyJob.mockImplementation(
    () =>
      new Promise<DormancyRun>((resolve) => {
        release = resolve;
      }),
  );
  mountScreen();

  const confirmButton = await armRun(user);
  await user.dblClick(confirmButton);
  expect(mocked.runDormancyJob).toHaveBeenCalledTimes(1);
  release(RUN_REPORT);

  // SPENT affordance: the verbatim server report replaces the action.
  const report = await screen.findByRole("status");
  expect(within(report).getByText("2026-08-06")).toBeInTheDocument();
  expect(within(report).getByText("2025-08-06")).toBeInTheDocument();
  expect(within(report).getByText("12")).toBeInTheDocument();
  expect(within(report).getByText("240")).toBeInTheDocument();
  expect(within(report).getByText("7")).toBeInTheDocument();
  expect(within(report).getByText("2")).toBeInTheDocument();
  const firstKey = mocked.runDormancyJob.mock.calls[0]?.[0];

  // The worklist truth is refetched after a successful run.
  await waitFor(() => expect(mocked.fetchMembersPage.mock.calls.length).toBeGreaterThan(1));

  // Intentional RE-RUN: close, reopen, re-confirm — a NEW intent with
  // a ROTATED key (the server must scan again, not replay the report).
  mocked.runDormancyJob.mockResolvedValue({ ...RUN_REPORT, transitioned: 0 });
  // Both the ✕ and the action button are honest closes.
  await user.click(screen.getAllByRole("button", { name: "Close" })[0]!);
  const confirmAgain = await armRun(user);
  await user.click(confirmAgain);
  await waitFor(() => expect(mocked.runDormancyJob).toHaveBeenCalledTimes(2));
  const secondKey = mocked.runDormancyJob.mock.calls[1]?.[0];
  expect(secondKey).toBeDefined();
  expect(secondKey).not.toBe(firstKey);
});

test("a 409 (dormancy period unconfigured — fail closed) is EXACTLY ONE attempt, rendered honestly, never replayed; the acknowledged conflict ROTATES the key (T3)", async () => {
  const user = userEvent.setup();
  mocked.runDormancyJob.mockRejectedValueOnce(new ApiError(409, "conflict", "corr-409"));
  mountScreen();

  const confirmButton = await armRun(user);
  await user.click(confirmButton);
  expect(
    await screen.findByText(/dormancy period is not\s+configured/),
  ).toBeInTheDocument();
  expect(mocked.runDormancyJob).toHaveBeenCalledTimes(1);
  const staleKey = mocked.runDormancyJob.mock.calls[0]?.[0];

  // Operator fixes Settings and retries in the SAME dialog: a NEW
  // intent — the rotated key must not be served the pinned 409.
  mocked.runDormancyJob.mockResolvedValue(RUN_REPORT);
  const dialog = screen.getByRole("dialog", { name: "Run dormancy scan" });
  await user.click(within(dialog).getByRole("button", { name: "Run dormancy scan…" }));
  const confirm = await screen.findByRole("dialog", { name: "Confirm dormancy scan" });
  await user.type(
    within(confirm).getByLabelText('Type "mark dormant" to confirm'),
    "mark dormant",
  );
  await user.click(within(confirm).getByRole("button", { name: "Run dormancy scan" }));
  await waitFor(() => expect(mocked.runDormancyJob).toHaveBeenCalledTimes(2));
  const freshKey = mocked.runDormancyJob.mock.calls[1]?.[0];
  expect(freshKey).toBeDefined();
  expect(freshKey).not.toBe(staleKey);
});

test("hostile member NAME renders as inert TEXT (named XSS threat)", async () => {
  mocked.fetchMembersPage.mockResolvedValue({
    items: [{ ...DORMANT_MEMBER, name: HOSTILE_NAME }],
    nextCursor: null,
  });
  const { container } = mountScreen();

  expect(await screen.findByText(HOSTILE_NAME)).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect((window as { __pwned?: unknown }).__pwned).toBeUndefined();
});
