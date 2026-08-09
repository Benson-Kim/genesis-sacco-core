/**
 * @jest-environment node
 *
 * #31 ledger (h3), wire side — the proactive refresh a skew-shrunk
 * (near-expiry) token triggers is SINGLE-FLIGHT: N concurrent
 * getValidAccessToken callers share exactly ONE token-endpoint POST
 * (no refresh stampede when a clock jump reclassifies the token), and
 * the rotated refresh token replaces the spent one atomically.
 *
 * Node environment (real fetch/Response globals; the reference
 * network-harness pattern: sessionStorage stubbed on a window shim,
 * fetch stubbed at the network boundary).
 */

export {};

const tabStorage = new Map<string, string>();
(globalThis as { window?: unknown }).window = {
  sessionStorage: {
    getItem: (key: string): string | null => tabStorage.get(key) ?? null,
    setItem: (key: string, value: string): void => {
      tabStorage.set(key, String(value));
    },
    removeItem: (key: string): void => {
      tabStorage.delete(key);
    },
    clear: (): void => {
      tabStorage.clear();
    },
    get length(): number {
      return tabStorage.size;
    },
  },
};

function b64url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function fakeJwt(exp: number): string {
  return `${b64url({ alg: "HS256" })}.${b64url({ sub: "user-1", exp })}.sig`;
}

let refreshCalls = 0;
let rotatedAccessToken = "";

async function fetchStub(input: Request | string | URL, init?: RequestInit): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input, init);
  const url = new URL(request.url);
  if (url.pathname === "/auth/refresh" && request.method === "POST") {
    refreshCalls += 1;
    return new Response(
      JSON.stringify({
        access_token: rotatedAccessToken,
        refresh_token: "rotated-refresh",
        expires_in: 900,
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }
  return new Response(JSON.stringify({ category: "not_found", correlation_id: "corr-n" }), {
    status: 404,
    headers: { "content-type": "application/json" },
  });
}

globalThis.fetch = fetchStub as typeof globalThis.fetch;
process.env.NEXT_PUBLIC_TENANT_ID = "22222222-2222-2222-2222-222222222222";

/* eslint-disable @typescript-eslint/no-require-imports */
const session = require("../session") as typeof import("../session");
/* eslint-enable @typescript-eslint/no-require-imports */

beforeEach(() => {
  refreshCalls = 0;
  session.clearSession();
});

afterEach(() => {
  session.clearSession();
});

test("three concurrent callers on a near-expiry token share EXACTLY ONE refresh POST; all receive the rotated token; the refresh token rotates in custody", async () => {
  const nowSec = Math.floor(Date.now() / 1000);
  rotatedAccessToken = fakeJwt(nowSec + 900);
  // 10s of validity left — inside the 30s margin (10 < 30, the
  // session.ts strict comparison; hand-computed).
  session.setSession({ accessToken: fakeJwt(nowSec + 10), refreshToken: "refresh-1" });

  const [a, b, c] = await Promise.all([
    session.getValidAccessToken(),
    session.getValidAccessToken(),
    session.getValidAccessToken(),
  ]);

  // Side-effect count at the wire: ONE refresh for three callers.
  expect(refreshCalls).toBe(1);
  expect(a).toBe(rotatedAccessToken);
  expect(b).toBe(rotatedAccessToken);
  expect(c).toBe(rotatedAccessToken);
  // The spent refresh token was replaced by the rotated one.
  expect(session.getRefreshToken()).toBe("rotated-refresh");
});

test("a FRESH token short-circuits: zero refresh calls, the in-memory token is returned as-is", async () => {
  const nowSec = Math.floor(Date.now() / 1000);
  const freshToken = fakeJwt(nowSec + 900);
  session.setSession({ accessToken: freshToken, refreshToken: "refresh-1" });

  await expect(session.getValidAccessToken()).resolves.toBe(freshToken);
  expect(refreshCalls).toBe(0);
});
