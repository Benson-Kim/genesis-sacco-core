/**
 * Users screen adversarial tests THROUGH THE REAL SCREEN CODE (ported
 * from !25 web/tests/users.test.mjs). The feature API layer is stubbed at
 * the module boundary; rendering, mutation orchestration, 409 handling,
 * idempotency-key custody and permission gating are the real shipped
 * code, mounted under the REAL <Providers> (so mutation retry:0 and the
 * 401 teardown are the production configuration — falsifiable: flip
 * mutations to retry, or auto-refetch on 409, and these tests fail).
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@genesis/api-client";
import { Providers } from "@/app/providers";
import { clearSession, hasSession, setSession } from "@/modules/auth/session";
import { usePermissions } from "@/modules/authz/usePermissions";
import { UsersScreen } from "../components/UsersScreen";
import * as usersApi from "../api";
import type { User } from "../schemas";

jest.mock("../api", () => {
  const actual = jest.requireActual<typeof import("../api")>("../api");
  return {
    ...actual,
    fetchUsersPage: jest.fn(),
    fetchUser: jest.fn(),
    fetchRoles: jest.fn(),
    createUser: jest.fn(),
    updateUser: jest.fn(),
    changeUserStatus: jest.fn(),
    assignUserRole: jest.fn(),
    invalidateUserOtp: jest.fn(),
    reenrolUserOtp: jest.fn(),
  };
});

jest.mock("@/modules/authz/usePermissions", () => ({
  usePermissions: jest.fn(),
}));

const mocked = jest.mocked(usersApi);
const mockedPermissions = jest.mocked(usePermissions);

const HOSTILE_NAME = "<img src=x onerror=alert(document.cookie)>";
const HOSTILE_BRANCH = '"?><script>window.__pwned=1</script>';
const USER_ID = "55555555-5555-5555-5555-555555555555";
const ADMIN_ID = "99999999-9999-9999-9999-999999999999";
const ROLE_TELLER = "66666666-6666-6666-6666-666666666666";
const ROLE_ADMIN = "77777777-7777-7777-7777-777777777777";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

function baseUser(overrides: Partial<User> = {}): User {
  return {
    id: USER_ID,
    full_name: HOSTILE_NAME,
    email: "hostile@sacco.co.ke",
    phone: null,
    branch: HOSTILE_BRANCH,
    role_id: ROLE_TELLER,
    role_name: "Teller",
    status: "active",
    last_active_at: null,
    version: 3,
    ...overrides,
  };
}

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
  role_id: ROLE_TELLER,
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
      <UsersScreen />
    </Providers>,
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  clearSession();
  setSession({ accessToken: fakeJwt(ADMIN_ID), refreshToken: "refresh-1" });
  grantPermissions(FULL_PERMS);
  mocked.fetchRoles.mockResolvedValue([
    { id: ROLE_TELLER, name: "Teller", is_system: true },
    { id: ROLE_ADMIN, name: "System Admin", is_system: true },
  ]);
  mocked.fetchUsersPage.mockResolvedValue({ items: [baseUser()], nextCursor: null });
  mocked.fetchUser.mockResolvedValue(baseUser());
});

afterEach(() => {
  clearSession();
});

test("hostile user fields render as inert text through the real screen (named XSS threat)", async () => {
  const { container } = mountScreen();
  // The hostile strings must appear byte-identical as TEXT…
  expect(await screen.findByText(HOSTILE_NAME)).toBeInTheDocument();
  expect(screen.getByText(HOSTILE_BRANCH)).toBeInTheDocument();
  // …and must NOT have been parsed into live elements. Falsifiable: route
  // any of these strings through a parser sink and img/script appear.
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect((window as unknown as Record<string, unknown>).__pwned).toBeUndefined();
});

test("stale edit: 409 shows the explicit reload flow with EXACTLY ONE write attempt", async () => {
  const user = userEvent.setup();
  mocked.updateUser.mockRejectedValue(new ApiError(409, "conflict", "corr-stale"));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Edit profile" }));
  const nameInput = await screen.findByLabelText("Full name");
  await user.clear(nameInput);
  await user.type(nameInput, "Renamed User");
  await user.click(screen.getByRole("button", { name: "Save changes" }));

  // Conflict banner + reload affordance; the stale value was NOT applied.
  expect(await screen.findByText(/Your change was NOT applied/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reload record" })).toBeInTheDocument();
  // Exactly one write attempt — no auto-retry, no silent overwrite with a
  // fresher version (falsifiable: set mutations retry>0 in Providers or
  // re-mutate on error and this count grows).
  expect(mocked.updateUser).toHaveBeenCalledTimes(1);
  expect(mocked.updateUser.mock.calls[0]?.[1]).toMatchObject({ version: 3 });

  // Explicit reload flow refreshes the record (server returns v4).
  mocked.fetchUser.mockResolvedValue(baseUser({ version: 4, full_name: "Server Renamed" }));
  await user.click(screen.getByRole("button", { name: "Reload record" }));
  expect(
    await screen.findByText(/Record reloaded — re-apply your change/),
  ).toBeInTheDocument();
  // Still exactly one write attempt: reload NEVER replays the submission.
  expect(mocked.updateUser).toHaveBeenCalledTimes(1);
});

test("idempotency keys: stable across retries of an identical body, rotated when the body changes", async () => {
  const user = userEvent.setup();
  // First two attempts fail with a retriable server error; the operator
  // retries the IDENTICAL submission, then changes the body and saves.
  mocked.updateUser
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-1"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "corr-2"))
    .mockResolvedValue(baseUser({ version: 4 }));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Edit profile" }));
  const nameInput = await screen.findByLabelText("Full name");
  await user.clear(nameInput);
  await user.type(nameInput, "Attempt One");

  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(mocked.updateUser).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(mocked.updateUser).toHaveBeenCalledTimes(2));

  const keyAttempt1 = mocked.updateUser.mock.calls[0]?.[2];
  const keyAttempt2 = mocked.updateUser.mock.calls[1]?.[2];
  // Identical logical submission => identical key (middleware replays).
  expect(keyAttempt1).toBe(keyAttempt2);

  // Changed body => rotated key (a NEW logical submission must never
  // replay the old stored response).
  await user.clear(nameInput);
  await user.type(nameInput, "Attempt Two");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(mocked.updateUser).toHaveBeenCalledTimes(3));
  const keyAttempt3 = mocked.updateUser.mock.calls[2]?.[2];
  expect(keyAttempt3).not.toBe(keyAttempt1);
});

test("UI affordances follow the matrix: view-only role sees no mutation controls", async () => {
  grantPermissions(VIEW_ONLY_PERMS);
  const user = userEvent.setup();
  mountScreen();

  expect(await screen.findByText(HOSTILE_NAME)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "+ Add user" })).toBeNull();

  await user.click(screen.getByText(HOSTILE_NAME));
  const drawer = await screen.findByRole("dialog", { name: "User detail" });
  expect(within(drawer).queryByRole("button", { name: "Edit profile" })).toBeNull();
  expect(within(drawer).queryByRole("button", { name: "Suspend" })).toBeNull();
  expect(within(drawer).queryByRole("button", { name: "Change role" })).toBeNull();
  expect(within(drawer).queryByRole("button", { name: /OTP/ })).toBeNull();
});

test("own row: role/status affordances disabled (separation-of-duties UX)", async () => {
  // Sign in AS the listed user.
  clearSession();
  setSession({ accessToken: fakeJwt(USER_ID), refreshToken: "refresh-1" });
  const user = userEvent.setup();
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  const drawer = await screen.findByRole("dialog", { name: "User detail" });
  expect(
    within(drawer).getByText(/Role and status changes require another/),
  ).toBeInTheDocument();
  expect(within(drawer).getByRole("button", { name: "Suspend" })).toBeDisabled();
  expect(within(drawer).getByRole("button", { name: "Change role" })).toBeDisabled();
  // Profile edit stays available — only role/status are separated.
  expect(within(drawer).getByRole("button", { name: "Edit profile" })).toBeEnabled();
});

test("OTP re-enrolment surfaces side-effect COUNTS only — zero OTP disclosure", async () => {
  const user = userEvent.setup();
  mocked.reenrolUserOtp.mockResolvedValue({
    voided_otp_challenges: 7,
    revoked_refresh_tokens: 9,
  });
  const { container } = mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Force OTP re-enrolment" }));
  const dialog = await screen.findByRole("dialog", { name: "Force re-enrolment" });
  await user.click(within(dialog).getByRole("button", { name: "Force re-enrolment" }));

  expect(await screen.findByText("Done.")).toBeInTheDocument();
  expect(screen.getByText("Voided OTP challenges")).toBeInTheDocument();
  expect(screen.getByText("7")).toBeInTheDocument();
  expect(screen.getByText("Revoked refresh tokens")).toBeInTheDocument();
  expect(screen.getByText("9")).toBeInTheDocument();
  // No 6-digit OTP-looking value anywhere in the DOM.
  expect(container.textContent).not.toMatch(/\b\d{6}\b/);
  // The mutation carried an Idempotency-Key.
  expect(mocked.reenrolUserOtp.mock.calls[0]?.[1]).toEqual(expect.any(String));
});

test("suspend flows through an explicit confirmation before any write", async () => {
  const user = userEvent.setup();
  mocked.changeUserStatus.mockResolvedValue(baseUser({ status: "suspended", version: 4 }));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  const drawer = await screen.findByRole("dialog", { name: "User detail" });
  await user.click(within(drawer).getByRole("button", { name: "Suspend" }));

  // No write yet — the confirmation dialog gates the destructive action.
  expect(mocked.changeUserStatus).not.toHaveBeenCalled();
  const dialog = await screen.findByRole("dialog", { name: "Suspend user" });
  expect(
    within(dialog).getByText(/pending OTP challenges are voided and every session is revoked/),
  ).toBeInTheDocument();
  await user.click(within(dialog).getByRole("button", { name: "Suspend user" }));

  await waitFor(() => expect(mocked.changeUserStatus).toHaveBeenCalledTimes(1));
  expect(mocked.changeUserStatus.mock.calls[0]?.[1]).toEqual({
    version: 3,
    status: "suspended",
  });
});

test("F1 email-change fence: 403 renders the standard least-disclosure banner, session intact", async () => {
  const user = userEvent.setup();
  // Backend review fix F1: an email change on an admin target by a
  // non-System-Admin is refused server-side with the sanitized envelope.
  mocked.updateUser.mockRejectedValue(new ApiError(403, "forbidden", "corr-f1"));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Edit profile" }));
  const emailInput = await screen.findByLabelText("Email");
  await user.clear(emailInput);
  await user.type(emailInput, "takeover@evil.example");
  await user.click(screen.getByRole("button", { name: "Save changes" }));

  // Standard ErrorBanner: sanitized per-status title + correlation id ONLY
  // (gate 1.6). Falsifiable: bypass ErrorBanner for the 403 branch (or echo
  // err.category/server detail) and these assertions fail.
  expect(await screen.findByText(/Not permitted\./)).toBeInTheDocument();
  expect(screen.getByText(/ref: corr-f1/)).toBeInTheDocument();
  // No capability-probing hint: the raw server category never renders.
  expect(screen.queryByText(/forbidden/i)).toBeNull();
  // 403 stays DISTINCT from 409 — the denial must NOT offer the
  // reload-and-retry conflict flow.
  expect(screen.queryByRole("button", { name: "Reload record" })).toBeNull();
  // Exactly one write attempt; a denial is never auto-retried.
  expect(mocked.updateUser).toHaveBeenCalledTimes(1);
  // Under-privilege is not session death: a 403 must NOT tear the session
  // down (falsifiable: widen the 401 teardown to 403 and this fails).
  expect(hasSession()).toBe(true);
});

test("F2 admin-grant fence: System-Admin role grant 403 renders the standard banner in the dialog", async () => {
  const user = userEvent.setup();
  // Backend review fix F2: granting System Admin requires the ACTOR's
  // users-table role to be System Admin — denials arrive as sanitized 403s.
  mocked.assignUserRole.mockRejectedValue(new ApiError(403, "forbidden", "corr-f2"));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Change role" }));
  const dialog = await screen.findByRole("dialog", { name: "Assign role" });
  await user.selectOptions(within(dialog).getByLabelText("New role"), ROLE_ADMIN);
  await user.click(within(dialog).getByRole("button", { name: "Assign role" }));

  // Falsifiable: drop the ErrorBanner from ConfirmActionDialog's error
  // branch and the denial becomes invisible to the operator.
  expect(await within(dialog).findByText(/Not permitted\./)).toBeInTheDocument();
  expect(within(dialog).getByText(/ref: corr-f2/)).toBeInTheDocument();
  expect(within(dialog).queryByText(/forbidden/i)).toBeNull();
  // Exactly one attempt with the loaded version — no retry on denial.
  expect(mocked.assignUserRole).toHaveBeenCalledTimes(1);
  expect(mocked.assignUserRole.mock.calls[0]?.[1]).toEqual({
    version: 3,
    role_id: ROLE_ADMIN,
  });
  expect(hasSession()).toBe(true);
});

test("F3 suspension: a query-path 401 kills the session immediately (no token-lifetime grace)", async () => {
  const user = userEvent.setup();
  // Backend review fix F3: RequirePermission verifies live users.status, so
  // the FIRST request after a committed suspension returns 401 even while
  // the access token is still unexpired. The QueryCache side of the
  // teardown must clear the session — the existing mutation-401 test alone
  // would let a suspended operator keep browsing read screens.
  mocked.fetchUser.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-f3"));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));

  // Falsifiable: remove the QueryCache onError in Providers (finding S3)
  // and the session survives the suspension. The timeout accommodates the
  // production query retry (retry: 1, ~1s backoff) — the teardown fires
  // only after the final failure, and that config must stay real here.
  await waitFor(() => expect(hasSession()).toBe(false), { timeout: 4000 });
});

test("mutation 401 tears the session down (re-login flow)", async () => {
  const user = userEvent.setup();
  mocked.updateUser.mockRejectedValue(new ApiError(401, "unauthenticated", "corr-401"));
  mountScreen();

  await user.click(await screen.findByText(HOSTILE_NAME));
  await user.click(await screen.findByRole("button", { name: "Edit profile" }));
  await user.click(await screen.findByRole("button", { name: "Save changes" }));

  // The MutationCache teardown (scaffold finding S3) clears the session;
  // falsifiable: remove the MutationCache onError and this keeps a live
  // session after a revoked-token mutation.
  await waitFor(() => expect(hasSession()).toBe(false));
});
