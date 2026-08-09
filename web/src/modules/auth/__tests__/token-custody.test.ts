/**
 * Token-custody proofs (gate 1.6), salvaged from
 * duo/feature/p13-5-frontend-followthrough @ 198a238 and ADAPTED to this
 * scaffold's P14 custody model (web/README.md "Security posture"):
 *
 * - The ACCESS token exists ONLY in JS module memory — it never touches
 *   any storage (localStorage would be readable forever by any injected
 *   script; even sessionStorage widens the XSS blast radius).
 * - The REFRESH token lives ONLY in per-tab sessionStorage under the
 *   single custody key — single-use, rotated with family-revocation on
 *   reuse (P3), so a stolen value dies on its first legitimate rotation.
 * - localStorage carries NOTHING.
 *
 * Falsifiable: persist the access token anywhere, move the refresh token
 * to localStorage, or write any extra key, and these tests fail.
 */
import {
  clearSession,
  decodeJwtPayload,
  getOwnUserId,
  getRefreshToken,
  hasSession,
  setSession,
} from "../session";

const ACCESS_SECRET_MARKER = "access-token-SECRET-A";
const REFRESH_SECRET_MARKER = "refresh-token-SECRET-R";
const REFRESH_TOKEN_KEY = "gp.refresh_token";

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

/** Fake JWT whose middle segment carries the marker via a claim. */
function fakeJwt(sub: string): string {
  const exp = Math.floor(Date.now() / 1000) + 3600;
  return `${b64url({ alg: "HS256" })}.${b64url({ sub, exp, marker: ACCESS_SECRET_MARKER })}.sig`;
}

afterEach(() => {
  clearSession();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

test("the access token NEVER reaches storage; the refresh token ONLY reaches its per-tab key", () => {
  const writes = jest.spyOn(Storage.prototype, "setItem");

  setSession({ accessToken: fakeJwt("user-1"), refreshToken: REFRESH_SECRET_MARKER });

  // Every storage write is the single refresh-token custody key.
  expect(writes).toHaveBeenCalledTimes(1);
  for (const call of writes.mock.calls) {
    expect(call[0]).toBe(REFRESH_TOKEN_KEY);
  }
  // localStorage carries nothing at all.
  expect(window.localStorage.length).toBe(0);
  // No storage value contains any access-token material.
  const dump =
    JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage });
  expect(dump).not.toContain(ACCESS_SECRET_MARKER);
  // The refresh token is exactly where the custody model puts it.
  expect(window.sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBe(REFRESH_SECRET_MARKER);
  expect(window.sessionStorage.length).toBe(1);
  // The session works.
  expect(hasSession()).toBe(true);
  expect(getRefreshToken()).toBe(REFRESH_SECRET_MARKER);
  writes.mockRestore();
});

test("clearSession drops every credential from memory AND storage", () => {
  setSession({ accessToken: fakeJwt("user-1"), refreshToken: REFRESH_SECRET_MARKER });
  clearSession();
  expect(hasSession()).toBe(false);
  expect(getRefreshToken()).toBeNull();
  expect(getOwnUserId()).toBeNull();
  expect(window.sessionStorage.length).toBe(0);
});

test("decodeJwtPayload reads sub for separation-of-duties UX and rejects garbage safely", () => {
  const payload = b64url({ sub: "user-1" });
  expect(decodeJwtPayload(`h.${payload}.s`)).toEqual({ sub: "user-1" });
  expect(decodeJwtPayload("not-a-jwt")).toBeNull();
  expect(decodeJwtPayload("a.%%%.c")).toBeNull();
  expect(decodeJwtPayload("a..c")).toBeNull();

  setSession({ accessToken: fakeJwt("user-9"), refreshToken: "r" });
  expect(getOwnUserId()).toBe("user-9");
});
