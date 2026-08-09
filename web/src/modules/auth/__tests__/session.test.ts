import {
  clearSession,
  getRefreshToken,
  hasSession,
  setSession,
  subscribe,
  tokenExpiresWithin,
} from "../session";

function fakeJwt(exp: number): string {
  const payload = btoa(JSON.stringify({ exp }))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return `header.${payload}.signature`;
}

describe("session store", () => {
  afterEach(() => {
    clearSession();
  });

  it("stores and clears the session", () => {
    expect(hasSession()).toBe(false);
    setSession({ accessToken: fakeJwt(9999999999), refreshToken: "refresh-token-value" });
    expect(hasSession()).toBe(true);
    expect(getRefreshToken()).toBe("refresh-token-value");
    clearSession();
    expect(hasSession()).toBe(false);
    expect(getRefreshToken()).toBeNull();
  });

  it("notifies subscribers on change", () => {
    const seen: boolean[] = [];
    const unsubscribe = subscribe(() => seen.push(hasSession()));
    setSession({ accessToken: fakeJwt(9999999999), refreshToken: "rt" });
    clearSession();
    unsubscribe();
    expect(seen).toEqual([true, false]);
  });

  it("treats far-future tokens as fresh and near-expiry tokens as stale", () => {
    const inOneHour = Math.floor(Date.now() / 1000) + 3600;
    const inFiveSeconds = Math.floor(Date.now() / 1000) + 5;
    expect(tokenExpiresWithin(fakeJwt(inOneHour), 30)).toBe(false);
    expect(tokenExpiresWithin(fakeJwt(inFiveSeconds), 30)).toBe(true);
  });

  it("treats malformed tokens as expired", () => {
    expect(tokenExpiresWithin("not-a-jwt", 30)).toBe(true);
    expect(tokenExpiresWithin("a.%%%.c", 30)).toBe(true);
  });
});

/**
 * #31 ledger (h3) — clock skew vs the 30-second proactive-refresh
 * margin, pinned deterministically with jest modern fake timers
 * (setSystemTime). The margin comparison is `expiresAt - now <
 * margin` (session.ts) — STRICT inequality; every oracle below is
 * hand-computed from that expression, never captured from the
 * implementation.
 */
describe("clock skew vs the 30s expiry margin (fake timers)", () => {
  afterEach(() => {
    clearSession();
    jest.useRealTimers();
  });

  it("boundary exactness: 31s and exactly 30s remaining are FRESH; 29s is STALE (strict <, hand-computed)", () => {
    jest.useFakeTimers({ now: new Date("2026-08-06T12:00:00.000Z") });
    const nowSec = Math.floor(Date.now() / 1000);
    // 31 - 0 < 30 → false; 30 - 0 < 30 → false; 29 - 0 < 30 → true.
    expect(tokenExpiresWithin(fakeJwt(nowSec + 31), 30)).toBe(false);
    expect(tokenExpiresWithin(fakeJwt(nowSec + 30), 30)).toBe(false);
    expect(tokenExpiresWithin(fakeJwt(nowSec + 29), 30)).toBe(true);
  });

  it("FORWARD skew (operator clock ahead of the issuer) fails SAFE: the token classifies stale EARLIER, forcing the proactive refresh", () => {
    jest.useFakeTimers({ now: new Date("2026-08-06T12:00:00.000Z") });
    const issuedAtSec = Math.floor(Date.now() / 1000);
    const token = fakeJwt(issuedAtSec + 300); // 5 real minutes left
    expect(tokenExpiresWithin(token, 30)).toBe(false);
    // The machine skews 271s ahead: apparent remaining = 29s < 30.
    jest.setSystemTime(new Date((issuedAtSec + 271) * 1000));
    expect(tokenExpiresWithin(token, 30)).toBe(true);
  });

  it("BACKWARD skew HONESTLY documented: a client clock behind the issuer can classify an already-expired token as fresh — the client hint is UX-only; the server verdict (401 → teardown, token-custody suite) is the enforcement", () => {
    jest.useFakeTimers({ now: new Date("2026-08-06T12:00:00.000Z") });
    const nowSec = Math.floor(Date.now() / 1000);
    // Token really expired 29s ago; the operator clock runs 60s slow:
    // apparent remaining = 60 - 29 = 31s ≥ 30 → classified fresh.
    const expired = fakeJwt(nowSec - 29);
    jest.setSystemTime(new Date((nowSec - 60) * 1000));
    expect(tokenExpiresWithin(expired, 30)).toBe(false);
    // The same token against the TRUE clock is stale — the hint's
    // blind spot is exactly the skew, never wider.
    jest.setSystemTime(new Date(nowSec * 1000));
    expect(tokenExpiresWithin(expired, 30)).toBe(true);
  });

  // The single-flight refresh under a near-expiry (skew-shrunk) token
  // needs REAL fetch/Response globals — it lives in the node-env
  // harness: session-refresh.network.test.ts.
});
