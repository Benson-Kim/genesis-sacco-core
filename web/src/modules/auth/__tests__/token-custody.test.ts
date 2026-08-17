/**
 * Token-custody proofs (gate 1.6, finding S1 — ported from !25
 * web/tests/session.test.mjs): credentials NEVER reach storage; only the
 * non-secret tenant id persists. Falsifiable: re-add any storage-backed
 * token persistence in session.ts and these tests fail.
 */
import {
  clearSession,
  decodeJwtPayload,
  getOwnUserId,
  getRefreshToken,
  getStoredTenantId,
  hasSession,
  setActiveTenantId,
  setSession,
  storeTenantId,
} from "../session";

const ACCESS = "access-token-SECRET-A";
const REFRESH = "refresh-token-SECRET-R";
const TENANT = "22222222-2222-2222-2222-222222222222";

afterEach(() => {
  clearSession();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

test("tokens NEVER reach storage; only the non-secret tenant id persists", () => {
  const localSet = jest.spyOn(Storage.prototype, "setItem");

  setActiveTenantId(TENANT);
  setSession({ accessToken: ACCESS, refreshToken: REFRESH });
  storeTenantId(TENANT);

  // Every storage write is the tenant key with the tenant UUID — nothing else.
  for (const call of localSet.mock.calls) {
    expect(call[0]).toBe("gp.tenant");
    expect(call[1]).toBe(TENANT);
  }
  expect(localSet).toHaveBeenCalledTimes(1);
  // Neither store contains any token substring.
  const dump = JSON.stringify({ ...window.localStorage }) + JSON.stringify({ ...window.sessionStorage });
  expect(dump).not.toContain("SECRET-A");
  expect(dump).not.toContain("SECRET-R");
  expect(window.sessionStorage.length).toBe(0);
  // The session itself works from memory.
  expect(hasSession()).toBe(true);
  expect(getRefreshToken()).toBe(REFRESH);
  expect(getStoredTenantId()).toBe(TENANT);
  localSet.mockRestore();
});

test("clearSession drops every credential (memory-only lifecycle)", () => {
  setSession({ accessToken: ACCESS, refreshToken: REFRESH });
  clearSession();
  expect(hasSession()).toBe(false);
  expect(getRefreshToken()).toBeNull();
  expect(getOwnUserId()).toBeNull();
});

test("decodeJwtPayload reads sub for UX and rejects garbage safely", () => {
  const payload = Buffer.from(JSON.stringify({ sub: "user-1" })).toString("base64url");
  expect(decodeJwtPayload(`h.${payload}.s`)).toEqual({ sub: "user-1" });
  expect(decodeJwtPayload("not-a-jwt")).toBeNull();
  expect(decodeJwtPayload("a.%%%.c")).toBeNull();
  expect(decodeJwtPayload("a..c")).toBeNull();

  setSession({
    accessToken: `h.${Buffer.from(JSON.stringify({ sub: "user-9" })).toString("base64url")}.s`,
    refreshToken: "r",
  });
  expect(getOwnUserId()).toBe("user-9");
});
