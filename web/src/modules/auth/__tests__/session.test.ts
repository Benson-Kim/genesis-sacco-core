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
