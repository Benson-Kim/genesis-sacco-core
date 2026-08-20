/**
 * Gate: design token VALUES are extracted VERBATIM from the prototype CSS
 * variables (P14 / MASTER_PROMPT §2.3). This test parses the `:root` block
 * of genesis_prestige_app.html and asserts byte-for-byte equality with
 * both tokens.ts and tokens.css. Names match the prototype except the
 * deliberate semantic renames in TOKEN_RENAMES below.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { colorTokens, cssVar } from "../tokens";

const REPO_ROOT = join(__dirname, "..", "..", "..", "..", "..");

/**
 * #37: the prototype names `--gold`/`--goldSoft` lie about their values
 * (#2E90FA is a blue, #DCEBFF a pale blue). Renamed to the semantic
 * `accent`/`accentSoft` matching actual usage (highlight/accent colour and
 * its soft pill background) WITHOUT changing the values — zero visual
 * diff by construction. The prototype oracle stays untouched; this map
 * records the rename so value drift in either direction still fails.
 */
const TOKEN_RENAMES: Record<string, string> = {
  gold: "accent",
  goldSoft: "accentSoft",
};

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

  it("match the prototype :root variables verbatim (values; names modulo the recorded #37 renames)", () => {
    const expected = Object.fromEntries(
      Object.entries(prototypeVars).map(([name, value]) => [
        TOKEN_RENAMES[name] ?? name,
        value,
      ]),
    );
    expect(colorTokens).toEqual(expected);
  });

  it("every recorded rename maps a real prototype token to a token that exists (no stale map entries)", () => {
    for (const [prototypeName, semanticName] of Object.entries(TOKEN_RENAMES)) {
      expect(prototypeVars).toHaveProperty(prototypeName);
      expect(colorTokens).toHaveProperty(semanticName);
      // The rename must be name-only: values stay byte-for-byte identical.
      expect(
        (colorTokens as Record<string, string>)[semanticName],
      ).toBe(prototypeVars[prototypeName]);
    }
  });

  it("tokens.css stays in sync with tokens.ts", () => {
    const tokensCss = readFileSync(join(__dirname, "..", "tokens.css"), "utf8");
    expect(parseRootVariables(tokensCss)).toEqual(colorTokens);
  });

  it("cssVar produces a custom-property reference", () => {
    expect(cssVar("navy")).toBe("var(--navy)");
  });
});
