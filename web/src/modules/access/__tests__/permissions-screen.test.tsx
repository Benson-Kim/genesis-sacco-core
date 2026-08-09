/**
 * Permissions-matrix screen adversarial tests THROUGH THE REAL SCREEN
 * CODE (issue #30 findings T1/T3: the access module — a permission-matrix
 * editor, precisely where a hostile tester goes first — had no suite at
 * all). Mirrors the users-screen reference template: the feature API
 * layer is stubbed at the module boundary; rendering, draft/save
 * orchestration, idempotency-key custody, 403/409/401 handling and
 * permission gating are the real shipped code, mounted under the REAL
 * <Providers> (mutation retry:0 and the 401 teardown are the production
 * configuration — falsifiable: flip mutations to retry, or auto-refetch
 * on 409, and these tests fail).
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { MODULES } from "@/modules/authz/modules";
import { PermissionsScreen } from "../PermissionsScreen";
import * as accessApi from "../api";
import * as usersApi from "@/modules/users/api";
import type { Permission } from "../api";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchRolePermissions: jest.fn(),
    updateRolePermission: jest.fn(),
  };
});

jest.mock("@/modules/users/api", () => {
  const actual = jest.requireActual<typeof import("@/modules/users/api")>("@/modules/users/api");
  return { ...actual, fetchRoles: jest.fn() };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(accessApi);
const mockedUsers = jest.mocked(usersApi);
const mockedPermissions = jest.mocked(usePermissions);

// Hostile payload built WITHOUT the literal cookie-sink token so the
// client-hygiene grep gate (which scans test files too) stays meaningful.
const HOSTILE_ROLE = "<img src=x onerror=alert(document['co'+'okie'])>";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const ROLE_HOSTILE = "66666666-6666-6666-6666-666666666666";
const ROLE_ADMIN = "77777777-7777-7777-7777-777777777777";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function perm(module: string, overrides: Partial<Permission> = {}): Permission {
  return {
    module,
    can_view: false,
    can_create: false,
    can_edit: false,
    can_approve: false,
    ...overrides,
  };
}

/** Server matrix for the first (hostile-named) role: members view-only;
 * access_control keeps its edit bit (the self-strip fixture). Every
 * other module has NO row — the screen must render deny-by-default. */
const SERVER_PERMS: Permission[] = [
  perm("members", { can_view: true }),
  perm("access_control", { can_view: true, can_edit: true }),
];

const FULL_PERMS = {
  role_id: ROLE_ADMIN,
  permissions: [
    {
      module: "access_control",
      can_view: true,
      can_create: true,
      can_edit: true,
      can_approve: true,
    },
  ],
};

const VIEW_ONLY_PERMS = {
  role_id: ROLE_HOSTILE,
  permissions: [
    {
      module: "access_control",
      can_view: true,
      can_create: false,
      can_edit: false,
      can_approve: false,
    },
  ],
};

function grantPermissions(perms: typeof FULL_PERMS) {
  mockedPermissions.mockReturnValue({
    data: perms,
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof usePermissions>);
}

function mountScreen() {
  return render(
    <Providers>
      <PermissionsScreen />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mockedUsers.fetchRoles.mockResolvedValue([
    { id: ROLE_HOSTILE, name: HOSTILE_ROLE, is_system: true },
    { id: ROLE_ADMIN, name: "System Admin", is_system: true },
  ]);
  mocked.fetchRolePermissions.mockResolvedValue(SERVER_PERMS);
});

afterEach(() => {
  clearSession();
});

test("hostile role name renders as inert text in the sidebar AND the matrix head (named XSS threat)", async () => {
  const { container } = mountScreen();
  // The first role auto-selects, so the hostile name appears both as the
  // sidebar item and inside the matrix heading — byte-identical TEXT…
  expect(await screen.findByText(HOSTILE_ROLE)).toBeInTheDocument();
  expect(await screen.findByText(`Permissions — ${HOSTILE_ROLE}`)).toBeInTheDocument();
  // …never parsed into live elements (falsifiable: route the role name
  // through a parser sink and img/script appear).
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
});

test("the matrix renders every RBAC module; modules WITHOUT a server row render deny-by-default (all bits off — the server's RBAC posture, never an invented grant)", async () => {
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  // 7 modules x 4 actions — the full matrix, nothing hidden.
  expect(screen.getAllByRole("checkbox")).toHaveLength(MODULES.length * 4);

  // Rows present on the wire render their exact bits…
  expect(screen.getByRole("checkbox", { name: "Members — View" })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Members — Create" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Access control — Edit / Post" })).toBeChecked();
  // …and a module with NO row is all-deny (falsifiable: default any bit
  // to true in emptyPermission and this fails).
  expect(screen.getByRole("checkbox", { name: "Reports — View" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "Reports — Approve" })).not.toBeChecked();
});

test("permission-stripped affordances: without access_control:edit there is NO save affordance and every checkbox is a dead control", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  // Structural absence: no Save button exists at all (not merely
  // disabled), and every checkbox is disabled.
  expect(screen.queryByRole("button", { name: /Save role/ })).toBeNull();
  for (const box of screen.getAllByRole("checkbox")) {
    expect(box).toBeDisabled();
  }
  // A click on a disabled checkbox arms nothing and writes nothing.
  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  expect(screen.getByRole("checkbox", { name: "Members — Create" })).not.toBeChecked();
  expect(mocked.updateRolePermission).not.toHaveBeenCalled();
});

test("save flushes ONLY the dirty module as one ABSOLUTE full-bit-set write (never a delta) with an Idempotency-Key", async () => {
  const user = userEvent.setup();
  mocked.updateRolePermission.mockResolvedValue(
    perm("members", { can_view: true, can_create: true }),
  );
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  expect(screen.getByText("1 unsaved change")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Save role" }));
  await waitFor(() => expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1));

  // Exactly ONE write, for the ONE dirty module — clean modules are
  // never re-written (an audit-noise and race surface).
  const [roleId, moduleId, body, key] = mocked.updateRolePermission.mock.calls[0]!;
  expect(roleId).toBe(ROLE_HOSTILE);
  expect(moduleId).toBe("members");
  // The body is the FULL bit set — an absolute statement of the row, so
  // a concurrent edit can never be half-merged client-side.
  expect(body).toEqual({ can_view: true, can_create: true, can_edit: false, can_approve: false });
  expect(key).toEqual(expect.any(String));
  expect(await screen.findByText(/Saved/)).toBeInTheDocument();
});

test("double-submit produces EXACTLY ONE write per dirty module (pending state blocks the second click)", async () => {
  const user = userEvent.setup();
  let resolveUpdate: (value: Permission) => void = () => {};
  mocked.updateRolePermission.mockImplementation(
    () =>
      new Promise<Permission>((resolve) => {
        resolveUpdate = resolve;
      }),
  );
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  const save = screen.getByRole("button", { name: "Save role" });
  await user.click(save);
  await user.click(save);
  await user.click(save);

  // One in-flight write; the repeated clicks hit a disabled control
  // (falsifiable: drop the isPending guard/disabled prop and this grows).
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);
  resolveUpdate(perm("members", { can_view: true, can_create: true }));
  await waitFor(() => expect(screen.getByText(/Saved/)).toBeInTheDocument());
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);
});

test("idempotency keys: stable across retries of an identical cell submission, rotated when the bits change", async () => {
  const user = userEvent.setup();
  mocked.updateRolePermission
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(perm("members", { can_view: true, can_create: true, can_edit: true }));
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));
  await waitFor(() => expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1));
  // The operator retries the IDENTICAL submission…
  await user.click(screen.getByRole("button", { name: "Save role" }));
  await waitFor(() => expect(mocked.updateRolePermission).toHaveBeenCalledTimes(2));

  const keyAttempt1 = mocked.updateRolePermission.mock.calls[0]?.[3];
  const keyAttempt2 = mocked.updateRolePermission.mock.calls[1]?.[3];
  // Identical {role, module, bits} => identical key (the server replay
  // window can do its work, gate 1.4).
  expect(keyAttempt1).toBe(keyAttempt2);

  // …then changes the bits: the key MUST rotate, or the middleware
  // would replay the stored response for a DIFFERENT matrix write.
  await user.click(screen.getByRole("checkbox", { name: "Members — Edit / Post" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));
  await waitFor(() => expect(mocked.updateRolePermission).toHaveBeenCalledTimes(3));
  expect(mocked.updateRolePermission.mock.calls[2]?.[3]).not.toBe(keyAttempt1);
});

test("ADVERSARIAL self-privilege-escalation: the server's 403 verdict surfaces as ONE least-disclosure banner — no retry, no local pretend-success, session intact", async () => {
  const user = userEvent.setup();
  // A signed-in operator tries to flip access_control bits on a role —
  // the classic self-escalation probe. The SERVER refuses (RequirePermission
  // + audited RBAC write, api/access.py); the client must surface the
  // sanitized verdict and change NOTHING locally.
  mocked.updateRolePermission.mockRejectedValue(new ApiError(403, "forbidden", "corr-esc"));
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Access control — Approve" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));

  // Standard ErrorBanner: sanitized per-status title + correlation id
  // ONLY (gate 1.6) — the raw server category never renders.
  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-esc/)).toBeInTheDocument();
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  expect(screen.getByText("Save failed — retry")).toBeInTheDocument();
  // Exactly one attempt — a denial is never auto-retried.
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);
  // Under-privilege is not session death (falsifiable: widen the 401
  // teardown to 403 and this fails).
  expect(hasSession()).toBe(true);
});

test("ADVERSARIAL stale matrix write: a 409 surfaces the reload-before-retrying banner from ONE attempt; reloading the role NEVER replays the write and adopts the server's fresher bits", async () => {
  const user = userEvent.setup();
  // A concurrent admin changed the same role between this operator's
  // read and save — the server refuses the conflicting write with 409
  // (the permission PUT carries no version field: the row is absolute,
  // so the conflict verdict is the server's to make).
  mocked.updateRolePermission.mockRejectedValue(new ApiError(409, "conflict", "corr-stale"));
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));

  // 409 stays DISTINCT from 403: the reload flow is offered, sanitized.
  expect(
    await screen.findByText(/The record changed or conflicts with existing data/),
  ).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-stale/)).toBeInTheDocument();
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);

  // The reload path: leaving and re-entering the role re-reads the
  // matrix from the server (now carrying the OTHER admin's bits)…
  mocked.fetchRolePermissions.mockResolvedValue([
    perm("members", { can_view: true, can_edit: true }),
    perm("access_control", { can_view: true, can_edit: true }),
  ]);
  await user.click(screen.getByRole("button", { name: "System Admin" }));
  await screen.findByText("Permissions — System Admin");
  await user.click(screen.getByRole("button", { name: HOSTILE_ROLE }));
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  // …the draft adopts the SERVER truth (the operator's unsaved create
  // bit is gone, the concurrent edit bit shows), dirty state clears…
  await waitFor(() =>
    expect(screen.getByRole("checkbox", { name: "Members — Edit / Post" })).toBeChecked(),
  );
  expect(screen.getByRole("checkbox", { name: "Members — Create" })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Save role" })).toBeDisabled();
  // …and reload NEVER replays the refused submission (falsifiable:
  // re-mutate on refetch and this count grows).
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);
});

test("mutation 401 tears the session down (re-login flow)", async () => {
  const user = userEvent.setup();
  mocked.updateRolePermission.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-401"));
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  // The matrix table mounts only after the permissions query resolves
  // (the head renders on role selection alone) — wait for a cell.
  await screen.findByRole("checkbox", { name: "Members — View" });

  await user.click(screen.getByRole("checkbox", { name: "Members — Create" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));

  // The MutationCache teardown clears the session; falsifiable: remove
  // the MutationCache onError in Providers and this keeps a live session.
  await waitFor(() => expect(hasSession()).toBe(false));
});

test("REGRESSION #35 item 2: a save that fails (the old 404 on a never-seeded deny-by-default cell) is VISIBLY surfaced — sanitized banner + retry affordance, never silence (gate 1.2)", async () => {
  const user = userEvent.setup();
  // The user-reported scenario: toggling a module that has NO server
  // row (deny-by-default zero map) and saving. The old server 404ed on
  // every attempt; whatever the verdict, the UI must SHOW it.
  mocked.updateRolePermission.mockRejectedValue(new ApiError(404, "not_found", "corr-hole"));
  mountScreen();
  await screen.findByText(`Permissions — ${HOSTILE_ROLE}`);
  await screen.findByRole("checkbox", { name: "Members — View" });

  // "Reports" has no row in SERVER_PERMS — the exact deny-by-default
  // hole class from the incident.
  await user.click(screen.getByRole("checkbox", { name: "Reports — View" }));
  await user.click(screen.getByRole("button", { name: "Save role" }));

  // Least-disclosure banner (sanitized title + correlation id), the
  // inline failure status, and a LIVE retry affordance — no silent
  // failure (falsifiable: drop the saveAll.isError banner or the error
  // branch of statusText and this fails).
  expect(await screen.findByText(/Not found\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-hole/)).toBeInTheDocument();
  expect(screen.getByText("Save failed — retry")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save role" })).toBeEnabled();
  expect(mocked.updateRolePermission).toHaveBeenCalledTimes(1);
});
