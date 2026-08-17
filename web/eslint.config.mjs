import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

// Zero-warning gate (MASTER_PROMPT §3): `npm run lint` runs with
// --max-warnings 0, so every warning below is a merge blocker.
const eslintConfig = [
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      "out/**",
      "coverage/**",
      "next-env.d.ts",
      // GENERATED from OpenAPI — never hand-edited (MASTER_PROMPT §2.1).
      "packages/api-client/src/generated/**",
    ],
  },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  // Injection-sink and token-custody guards (gate 1.6 — scaffold review
  // finding S4). Audit payloads and user profile fields are attacker-
  // influenced data: the render path must have NO parser sink, and no
  // credential may ever reach script-readable storage or the console.
  // Each rule is mirrored by a falsifiable jest test (no-sinks.test.ts).
  {
    files: ["src/**/*.{ts,tsx}", "packages/**/*.{ts,tsx}"],
    ignores: ["**/__tests__/**"],
    rules: {
      "react/no-danger": "error",
      "no-console": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { property: "outerHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { property: "insertAdjacentHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { object: "document", property: "write", message: "Parser sink (XSS gate 1.6)." },
        { object: "document", property: "writeln", message: "Parser sink (XSS gate 1.6)." },
        { object: "window", property: "localStorage", message: "Token custody: storage only in auth/session.ts (non-secret tenant id only)." },
        { object: "window", property: "sessionStorage", message: "Token custody: no session storage anywhere (memory-only tokens)." },
        { object: "globalThis", property: "localStorage", message: "Token custody: storage only in auth/session.ts." },
        { object: "globalThis", property: "sessionStorage", message: "Token custody: no session storage anywhere." },
      ],
      "no-restricted-globals": [
        "error",
        { name: "localStorage", message: "Token custody: storage only in auth/session.ts (non-secret tenant id only)." },
        { name: "sessionStorage", message: "Token custody: no session storage anywhere (memory-only tokens)." },
      ],
    },
  },
  // The single allowlisted storage site: persists ONLY the non-secret
  // tenant id (routing config). Parser-sink rules stay active here.
  {
    files: ["src/modules/auth/session.ts"],
    rules: {
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { property: "outerHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { property: "insertAdjacentHTML", message: "Parser sink — build DOM via React only (XSS gate 1.6)." },
        { object: "document", property: "write", message: "Parser sink (XSS gate 1.6)." },
        { object: "document", property: "writeln", message: "Parser sink (XSS gate 1.6)." },
        { object: "window", property: "sessionStorage", message: "Token custody: no session storage anywhere (memory-only tokens)." },
        { object: "globalThis", property: "sessionStorage", message: "Token custody: no session storage anywhere." },
      ],
      "no-restricted-globals": [
        "error",
        { name: "sessionStorage", message: "Token custody: no session storage anywhere (memory-only tokens)." },
      ],
    },
  },
];

export default eslintConfig;
