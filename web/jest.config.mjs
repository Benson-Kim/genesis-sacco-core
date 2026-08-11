import nextJest from "next/jest.js";

const createJestConfig = nextJest({ dir: "./" });

/** @type {import('jest').Config} */
const config = {
  testEnvironment: "jsdom",
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  // The screen suites drive real userEvent typing through multi-step
  // drawer flows (lookup → blur → resolve → confirm), which routinely
  // exceeds jest's 5s default on a loaded runner — the failures were
  // timeouts, never assertion failures. Raising the ceiling changes no
  // assertion; a genuinely hung test still fails, just later.
  testTimeout: 30000,
  moduleNameMapper: {
    "^@genesis/design-system$": "<rootDir>/packages/design-system/src",
    "^@genesis/api-client$": "<rootDir>/packages/api-client/src",
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  // e2e/ is Playwright's tree (web:e2e job) — jest must not collect it.
  testPathIgnorePatterns: ["<rootDir>/node_modules/", "<rootDir>/.next/", "<rootDir>/e2e/"],
};

export default createJestConfig(config);
