/**
 * Accessibility source gate (P15 Phase B — jest-axe-equivalent static
 * checks; npm registry is proxy-blocked for the sandbox, so this gate is
 * hand-rolled with zero new dependencies). It enforces the issue-#8
 * conventions that ARE statically checkable:
 *
 *  - focus rings are REPLACED, never removed: any stylesheet that removes
 *    an outline must provide a :focus-visible replacement in the same file;
 *  - no positive tabIndex (tab order must follow DOM order);
 *  - no autoFocus attributes (the Modal owns initial focus; autofocus
 *    steals it unpredictably);
 *  - every aria-live region is paired with aria-atomic (partial reads are
 *    worse than silence).
 *
 * Falsifiability: synthetic offenders below prove each rule fires.
 * Rendered-behaviour checks (labels, roles, focus traps) live in the
 * component suites — this gate covers what grep can prove.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

const WEB_ROOT = join(__dirname, "..", "..");
const SCAN_ROOTS = [join(WEB_ROOT, "src"), join(WEB_ROOT, "packages")];
const SELF = join("src", "__tests__", "a11y-gate.test.ts");

function collectFiles(root: string, extensions: readonly string[]): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(root)) {
    const path = join(root, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "node_modules" || entry === ".next" || entry === "generated") continue;
      files.push(...collectFiles(path, extensions));
    } else if (extensions.some((ext) => path.endsWith(ext))) {
      files.push(path);
    }
  }
  return files;
}

function rel(path: string): string {
  return relative(WEB_ROOT, path).split(sep).join("/");
}

const OUTLINE_REMOVAL = /outline\s*:\s*(?:none|0)\b/;
const FOCUS_VISIBLE = /:focus-visible/;
const POSITIVE_TABINDEX = /tabIndex=\{?\s*[1-9]/;
const AUTOFOCUS = /\bautoFocus\b/;
// Attribute USAGE only (aria-live=) — prose mentions in comments are fine.
const ARIA_LIVE = /aria-live=/;
const ARIA_ATOMIC = /aria-atomic=/;

describe("a11y source gate (issue #8)", () => {
  const cssFiles = SCAN_ROOTS.flatMap((root) => collectFiles(root, [".css"]));
  const tsxFiles = SCAN_ROOTS.flatMap((root) => collectFiles(root, [".ts", ".tsx"])).filter(
    (path) => rel(path) !== SELF.split(sep).join("/") && !rel(path).includes("__tests__/"),
  );

  it("scans a non-trivial tree (the gate cannot pass vacuously)", () => {
    expect(cssFiles.length).toBeGreaterThan(10);
    expect(tsxFiles.length).toBeGreaterThan(30);
  });

  it("flags synthetic offenders (the gate itself is falsifiable)", () => {
    expect(OUTLINE_REMOVAL.test(".btn { outline: none; }")).toBe(true);
    expect(OUTLINE_REMOVAL.test(".btn { outline: 0; }")).toBe(true);
    expect(POSITIVE_TABINDEX.test("<div tabIndex={3} />")).toBe(true);
    expect(POSITIVE_TABINDEX.test("<div tabIndex={-1} />")).toBe(false);
    expect(AUTOFOCUS.test("<input autoFocus />")).toBe(true);
    expect(ARIA_LIVE.test('<div aria-live="polite" />')).toBe(true);
    expect(ARIA_LIVE.test("// prose about the aria-live region")).toBe(false);
  });

  it("focus rings are replaced, never removed (outline removal requires :focus-visible)", () => {
    const offenders = cssFiles
      .filter((path) => {
        const css = readFileSync(path, "utf8");
        return OUTLINE_REMOVAL.test(css) && !FOCUS_VISIBLE.test(css);
      })
      .map(rel);
    expect(offenders).toEqual([]);
  });

  it("no positive tabIndex anywhere (tab order follows DOM order)", () => {
    const offenders = tsxFiles.filter((path) => POSITIVE_TABINDEX.test(readFileSync(path, "utf8"))).map(rel);
    expect(offenders).toEqual([]);
  });

  it("no autoFocus attributes (the Modal owns initial focus)", () => {
    const offenders = tsxFiles.filter((path) => AUTOFOCUS.test(readFileSync(path, "utf8"))).map(rel);
    expect(offenders).toEqual([]);
  });

  it("every aria-live region is aria-atomic", () => {
    const offenders = tsxFiles
      .filter((path) => {
        const source = readFileSync(path, "utf8");
        return ARIA_LIVE.test(source) && !ARIA_ATOMIC.test(source);
      })
      .map(rel);
    expect(offenders).toEqual([]);
  });
});
