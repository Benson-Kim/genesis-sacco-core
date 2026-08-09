/**
 * Gate: design tokens are extracted VERBATIM from the prototype CSS
 * variables (P14 / MASTER_PROMPT §2.3). This test parses the `:root` block
 * of genesis_prestige_app.html and asserts byte-for-byte equality with
 * both tokens.ts and tokens.css.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { colorTokens, cssVar } from "../tokens";

const REPO_ROOT = join(__dirname, "..", "..", "..", "..", "..");

function parseRootVariables(css: string): Record<string, string> {
  const rootMatch = /:root\s*\{([\s\S]*?)\}/.exec(css);
  if (!rootMatch || rootMatch[1] === undefined) {
    throw new Error("no :root block found");
  }
  const variables: Record<string, string> = {};
  const varPattern = /--([A-Za-z][A-Za-z0-9]*)\s*:\s*([^;}]+)/g;
  let match: RegExpExecArray | null;
  while ((match = varPattern.exec(rootMatch[1])) !== null) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined) {
      variables[name] = value.trim();
    }
  }
  return variables;
}

describe("design tokens", () => {
  const prototypeHtml = readFileSync(
    join(REPO_ROOT, "genesis_prestige_app.html"),
    "utf8",
  );
  const prototypeVars = parseRootVariables(prototypeHtml);

  it("match the prototype :root variables verbatim (names and values)", () => {
    expect(colorTokens).toEqual(prototypeVars);
  });

  it("tokens.css stays in sync with tokens.ts", () => {
    const tokensCss = readFileSync(join(__dirname, "..", "tokens.css"), "utf8");
    expect(parseRootVariables(tokensCss)).toEqual(colorTokens);
  });

  it("cssVar produces a custom-property reference", () => {
    expect(cssVar("navy")).toBe("var(--navy)");
  });
});
