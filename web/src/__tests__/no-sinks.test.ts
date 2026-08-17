/**
 * @jest-environment node
 *
 * Injection-sink and token-custody source gate (gate 1.6), ported from the
 * !25 lint suite. Every forbidden token below is a named threat defence:
 * remove a guard (e.g. render a payload via dangerouslySetInnerHTML, or
 * persist a token to sessionStorage) and this suite FAILS — falsifiability
 * per MASTER_PROMPT §4. The eslint config enforces the same rules at lint
 * time; this test keeps the defence falsifiable from the test stage too.
 */
import { readdirSync, readFileSync, statSync } from "fs";
import { join, relative, sep } from "path";

const ROOT = join(__dirname, "..", "..");

/** Source trees that render or transport attacker-influenced data. */
const SCAN_DIRS = [join(ROOT, "src"), join(ROOT, "packages")];

/** file suffix => scanned. Tests and generated code are excluded. */
function isSource(path: string): boolean {
  if (!/\.(ts|tsx)$/.test(path)) return false;
  const rel = relative(ROOT, path).split(sep).join("/");
  if (rel.includes("__tests__/")) return false;
  if (rel.includes("packages/api-client/src/generated/")) return false;
  return true;
}

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === ".next") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

const sources = SCAN_DIRS.flatMap(walk).filter(isSource);

interface Rule {
  pattern: RegExp;
  reason: string;
  /** repo-relative paths where the pattern is deliberately allowed. */
  allow?: string[];
}

const RULES: Rule[] = [
  { pattern: /dangerouslySetInnerHTML/, reason: "React parser sink (named XSS threat)" },
  { pattern: /\binnerHTML\b/, reason: "HTML parser sink" },
  { pattern: /\bouterHTML\b/, reason: "HTML parser sink" },
  { pattern: /insertAdjacentHTML/, reason: "HTML parser sink" },
  { pattern: /document\.write/, reason: "HTML parser sink" },
  { pattern: /\beval\s*\(/, reason: "code-execution sink" },
  { pattern: /new\s+Function\s*\(/, reason: "code-execution sink" },
  { pattern: /sessionStorage/, reason: "token custody: memory-only tokens (finding S1)" },
  {
    pattern: /localStorage/,
    reason: "token custody: storage only for the non-secret tenant id",
    allow: ["src/modules/auth/session.ts"],
  },
  {
    pattern: /console\.(log|info|warn|error|debug|trace)/,
    reason: "no PII/token leakage into logs (gate 1.6)",
  },
];

describe("no injection sinks or token-custody violations in shipped source", () => {
  it("scans a non-trivial source tree (sanity: the gate cannot pass vacuously)", () => {
    expect(sources.length).toBeGreaterThan(20);
  });

  for (const rule of RULES) {
    it(`forbids ${rule.pattern} (${rule.reason})`, () => {
      const offenders: string[] = [];
      for (const file of sources) {
        const rel = relative(ROOT, file).split(sep).join("/");
        if (rule.allow?.includes(rel)) continue;
        const text = readFileSync(file, "utf8");
        if (rule.pattern.test(text)) offenders.push(rel);
      }
      expect(offenders).toEqual([]);
    });
  }
});
