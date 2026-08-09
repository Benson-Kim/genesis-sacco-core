/**
 * Inactivity sign-out — falsifiable legs (#35).
 *
 * Hand-computed oracle: IDLE_WINDOW_SECONDS = 900, IDLE_WARNING_SECONDS
 * = 60, tick 1 s. So: warning visible from t=840 s of inactivity;
 * expiry at t=900 s fires EXACTLY ONE `POST /auth/logout` carrying the
 * per-tab refresh token; any tracked activity before t=900 restarts the
 * walk from 0.
 *
 * The wire is mocked at the generated-client boundary (`@/lib/api`), so
 * the single-revocation and zero-call proofs measure real request
 * effects through the REAL `logout()` custody (session cleared in its
 * `finally`), not component internals. Remove the single-flight guard,
 * let ambient activity dismiss the warning, or skip the store flip, and
 * these tests fail.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { IdleLogoutGuard } from "../components/IdleLogoutGuard";
import { RequireAuth } from "../components/RequireAuth";
import { clearSession, hasSession, setSession } from "../session";
import { IDLE_WARNING_SECONDS, IDLE_WINDOW_SECONDS } from "../idleLogout";

const mockPost = jest.fn();
jest.mock("@/lib/api", () => ({
  api: { POST: (...args: unknown[]) => mockPost(...args) as unknown },
}));

const mockReplace = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mockReplace }),
}));

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

/** Fake JWT — long-lived so the idle window, not token expiry, is under test. */
function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 24 * 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp })}.sig`;
}

const WINDOW_MS = IDLE_WINDOW_SECONDS * 1000;
const WARNING_MS = IDLE_WARNING_SECONDS * 1000;

beforeEach(() => {
  jest.useFakeTimers();
});

afterEach(() => {
  clearSession();
  window.sessionStorage.clear();
  jest.useRealTimers();
  jest.clearAllMocks();
});

async function advance(ms: number): Promise<void> {
  await act(async () => {
    jest.advanceTimersByTime(ms);
  });
}

test("idle expiry: EXACTLY ONE revocation through the existing logout path; credentials cleared; RequireAuth lands on the login gate", async () => {
  mockPost.mockResolvedValue({});
  setSession({ accessToken: fakeJwt("user-1"), refreshToken: "refresh-SECRET-R1" });

  render(
    <RequireAuth>
      <IdleLogoutGuard />
      <div>protected content</div>
    </RequireAuth>,
  );
  expect(await screen.findByText("protected content")).toBeInTheDocument();

  // Walk straight through the warning band to expiry (+1 tick of slack).
  await advance(WINDOW_MS + 1000);

  // Exactly one wire revocation, carrying THIS tab's refresh token.
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(mockPost).toHaveBeenCalledWith("/auth/logout", {
    body: { refresh_token: "refresh-SECRET-R1" },
  });
  // Client custody died with it (access token memory + per-tab key).
  expect(hasSession()).toBe(false);
  // The store flip took the tab to the login gate (existing mechanism).
  expect(mockReplace).toHaveBeenCalledWith("/login");
  expect(screen.queryByText("protected content")).toBeNull();

  // Later ticks NEVER re-fire the revocation (single-flight guard).
  await advance(10_000);
  expect(mockPost).toHaveBeenCalledTimes(1);
});

test("activity resets the window: ZERO logout calls even after the original deadline has long passed", async () => {
  mockPost.mockResolvedValue({});
  setSession({ accessToken: fakeJwt("user-1"), refreshToken: "refresh-SECRET-R2" });
  render(<IdleLogoutGuard />);

  // 2 minutes before the warning would appear…
  await advance(WINDOW_MS - WARNING_MS - 120_000);
  // …the operator types (tracked, throttled activity).
  fireEvent.keyDown(document.body, { key: "a" });

  // Sail far past the ORIGINAL deadline (t = 720 s + 810 s = 1530 s >
  // 900 s) while staying under the RESTARTED window (810 s idle < 840 s
  // warning threshold): still quiet, still signed in.
  await advance(WINDOW_MS - WARNING_MS - 30_000);
  expect(mockPost).not.toHaveBeenCalled();
  expect(hasSession()).toBe(true);
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

test("final-minute warning: focus-trapped alertdialog; ambient activity never dismisses it; 'Stay signed in' restarts the walk from zero", async () => {
  mockPost.mockResolvedValue({});
  const user = userEvent.setup({ advanceTimers: (ms) => jest.advanceTimersByTime(ms) });
  setSession({ accessToken: fakeJwt("user-1"), refreshToken: "refresh-SECRET-R3" });
  render(<IdleLogoutGuard />);

  // Enter the warning band (30 s of it left).
  await advance(WINDOW_MS - 30_000);
  const dialog = screen.getByRole("alertdialog", { name: "Still there?" });
  // The shared Modal moved focus INTO the dialog (keyboard parity).
  expect(dialog.contains(document.activeElement)).toBe(true);

  // Ambient pointer movement is NOT an explicit decision — the warning stays.
  fireEvent.pointerMove(window);
  await advance(1000);
  expect(screen.getByRole("alertdialog", { name: "Still there?" })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Stay signed in" }));
  expect(screen.queryByRole("alertdialog")).toBeNull();

  // The walk restarted from ZERO: a full fresh window passes before expiry…
  await advance(WINDOW_MS - WARNING_MS);
  expect(mockPost).not.toHaveBeenCalled();
  // …and then expiry fires exactly once.
  await advance(WARNING_MS + 1000);
  expect(mockPost).toHaveBeenCalledTimes(1);
  expect(hasSession()).toBe(false);
});

test("fail-closed: a wire-dead revocation still ends in cleared client credentials (logout()'s finally custody)", async () => {
  mockPost.mockRejectedValue(new Error("network unreachable"));
  setSession({ accessToken: fakeJwt("user-1"), refreshToken: "refresh-SECRET-R4" });
  render(<IdleLogoutGuard />);

  await advance(WINDOW_MS + 1000);

  expect(mockPost).toHaveBeenCalledTimes(1);
  // The revocation call died at the wire — custody is cleared anyway.
  expect(hasSession()).toBe(false);
  expect(window.sessionStorage.getItem("gp.refresh_token")).toBeNull();
});
