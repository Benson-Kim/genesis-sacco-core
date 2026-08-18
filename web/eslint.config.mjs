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
  {
    rules: {
      // FM3 (P14): console output is a PII/token leak channel in a banking
      // client — sanitized error envelopes render in the UI instead.
      "no-console": "error",
    },
  },
];

export default eslintConfig;
