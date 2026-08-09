import { defineConfig } from "@playwright/test";

/**
 * Playwright E2E configuration (P15 exit criterion). Runs against the
 * PRODUCTION build (`next start` — real middleware/CSP/headers). The
 * backend API is mocked at the BROWSER network boundary per spec
 * (page.route on the API origin): the real generated client, session
 * custody, guards and screens execute in a real browser; evidence is
 * the in-project `web:e2e` pipeline job (blocker (e)).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  // One deliberate attempt per test — a flaky retry could mask a real
  // double-submit/idempotency regression (the failure modes under test).
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run start",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
