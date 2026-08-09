import nextJest from "next/jest.js";

const createJestConfig = nextJest({ dir: "./" });

/** @type {import('jest').Config} */
const config = {
  testEnvironment: "jsdom",
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  moduleNameMapper: {
    "^@genesis/design-system$": "<rootDir>/packages/design-system/src",
    "^@genesis/api-client$": "<rootDir>/packages/api-client/src",
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  // e2e/ is Playwright's tree (web:e2e job) — jest must not collect it.
  testPathIgnorePatterns: ["<rootDir>/node_modules/", "<rootDir>/.next/", "<rootDir>/e2e/"],
};

export default createJestConfig(config);
