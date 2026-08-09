/**
 * @jest-environment node
 *
 * Injection-sink and token-custody source gate (gate 1.6), salvaged from
 * duo/feature/p13-5-frontend-followthrough @ 198a238 and ADAPTED to this
 * scaffold's custody model. Every forbidden token below is a named threat
 * defence: remove a guard (e.g. render a payload via
 * dangerouslySetInnerHTML, or persist a token outside the single custody
 * site) and this suite FAILS — falsifiability per MASTER_PROMPT §4.
 *
 * Local-storage / console / analytics / CDN hygiene is owned by the
 * client-hygiene gate (src/__tests__/client-hygiene.test.ts) — one copy
 * of each gate (gate 1.1); this suite owns the PARSER/CODE-EXECUTION
 * sinks and the sessionStorage custody allowlist.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

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
  {
    pattern: /sessionStorage/,
    reason:
      "token custody: the per-tab refresh token lives ONLY in the single custody site (web/README.md security posture)",
    allow: ["src/modules/auth/session.ts"],
  },
];

describe("no injection sinks or token-custody violations in shipped source", () => {
  it("scans a non-trivial source tree (sanity: the gate cannot pass vacuously)", () => {
    expect(sources.length).toBeGreaterThan(20);
  });

  it("flags synthetic offenders (the gate itself is falsifiable)", () => {
    // One representative offender per rule — hand-written, never captured
    // from the implementation. Removing a rule fails the length check;
    // neutering its regex fails its match check.
    const samples: readonly string[] = [
      "<div dangerouslySetInnerHTML={{ __html: payload }} />",
      "node.innerHTML = payload;",
      "node.outerHTML = payload;",
      "node.insertAdjacentHTML('beforeend', payload);",
      "document.write(payload);",
      "eval(payload);",
      "const fn = new Function(payload);",
      "window.sessionStorage.setItem('token', accessToken);",
    ];
    expect(RULES).toHaveLength(samples.length);
    RULES.forEach((rule, index) => {
      expect(rule.pattern.test(samples[index] ?? "")).toBe(true);
    });
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
