/**
 * Narrative-hygiene gate — the RENDERED product surface must be free of
 * engineering-process provenance. Screen copy comes from the product
 * vocabulary (the design prototype is the copy truth); work-item ids,
 * MR ids, batch numbers, audit/finding codes, plan codes and doctrine
 * citations are engineering narrative and must never reach a user.
 *
 * This gate scans RENDERED-STRING SOURCES ONLY: string literals
 * (labels, titles, aria-labels, placeholders, toast/message constants)
 * and JSX text nodes. Code comments are NOT rendered and are therefore
 * out of scope here (they are swept editorially, not gated).
 *
 * The banned list is code-owned. The backend twin of this gate is
 * backend/tests/test_description_hygiene.py (the OpenAPI description
 * surface) — keep the two lists aligned when extending either.
 *
 * Falsifiability: every banned pattern must flag its synthetic offender
 * sample below — removing a pattern (or weakening its regex) fails that
 * leg, so the gate can never pass vacuously.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

const WEB_ROOT = join(__dirname, "..", "..");
const SCAN_ROOTS = [join(WEB_ROOT, "src"), join(WEB_ROOT, "packages")];
const EXTENSIONS = [".ts", ".tsx"];

/**
 * Engineering-provenance patterns banned from rendered copy.
 *
 * Judgement calls (documented so the list stays honest):
 *  - Issue refs are `#` + 1-2 digits with no hex digit following:
 *    longer or hex-lettered runs are CSS colours (`#333`, `#0F2C6B`),
 *    which are not provenance.
 *  - Bare review codes `R<n>`/`A<n>` are NOT gated standalone — they
 *    collide with real-world tokens (paper sizes, shortcut labels).
 *    Review provenance travels with the other codes, which are gated.
 *  - `GP-` is the member-number format, NOT a plan code — never ban it.
 */
const BANNED: readonly { name: string; regex: RegExp }[] = [
    { name: "work-item reference", regex: /(?<![\w&#])#\d{1,2}(?![\dA-Fa-f])|\bissue\s*#\d+/i },
    { name: "merge-request reference", regex: /(?<![\w!])!\d{1,3}(?!\d)/ },
    { name: "batch number", regex: /\bbatch[ -]\d+\b/i },
    { name: "audit finding code", regex: /\bDA-\d+|\bB13-R\d\b|\bFM\d\b|\bN[1-9]\b|\bU[1-6]\b|\bT5\b|\bW[1-4]\b|\bX[34]\b/ },
    { name: "plan code", regex: /\bP1[0-9]\.\d+\b|\bP15\b|\bP22\b/ },
    { name: "doctrine citation", regex: /\brule 1[0-9]\b|\bgate 1\.[1-6]\b|MASTER_PROMPT|BUILD_PROMPTS|§\d/i },
    { name: "process vocabulary", regex: /\bledger \([a-o]\)|\baudit #|\bprototype\b|\bhand-off\b|\bre-import\b/i },
];

/** Synthetic offenders proving each pattern is live (one per pattern). */
const OFFENDER_SAMPLES: Readonly<Record<string, string>> = {
    "work-item reference": "Follow-up recorded on issue #31",
    "merge-request reference": "Fixed in !52 review",
    "batch number": "Added by batch 8 remediation",
    "audit finding code": "Per finding B13-R4 and FM2",
    "plan code": "P13.5 frontend branch scope",
    "doctrine citation": "Least disclosure (gate 1.6) applies",
    "process vocabulary": "Matches the prototype exit checklist",
};

/**
 * Extract the rendered-string surface of a TS/TSX source file:
 * string/template literal CONTENTS plus JSX text nodes, with comments
 * removed (comments are not rendered). Single-pass character scanner —
 * no parser dependency.
 */
export function extractRenderedStrings(source: string): string[] {
    const strings: string[] = [];
    const codeOnly: string[] = [];
    type State = "code" | "line" | "block" | "single" | "double" | "template";
    let state: State = "code";
    let buffer = "";
    for (let i = 0; i < source.length; i += 1) {
        const ch = source.charAt(i);
        const next = source.charAt(i + 1);
        if (state === "code") {
            if (ch === "/" && next === "/") {
                state = "line";
                i += 1;
            } else if (ch === "/" && next === "*") {
                state = "block";
                i += 1;
            } else if (ch === "'") {
                state = "single";
                buffer = "";
            } else if (ch === '"') {
                state = "double";
                buffer = "";
            } else if (ch === "`") {
                state = "template";
                buffer = "";
            } else {
                codeOnly.push(ch);
            }
        } else if (state === "line") {
            if (ch === "\n") {
                state = "code";
                codeOnly.push("\n");
            }
        } else if (state === "block") {
            if (ch === "*" && next === "/") {
                state = "code";
                i += 1;
            } else if (ch === "\n") {
                codeOnly.push("\n");
            }
        } else {
            // Inside a string/template literal.
            if (ch === "\\") {
                buffer += ch + (next ?? "");
                i += 1;
            } else if (
                (state === "single" && ch === "'") ||
                (state === "double" && ch === '"') ||
                (state === "template" && ch === "`")
            ) {
                strings.push(buffer);
                state = "code";
            } else {
                buffer += ch;
            }
        }
    }
    // JSX text nodes: literal text between tags/expressions in the
    // comment- and string-blanked code. Text may be delimited by a tag
    // (`>`/`<`) OR an expression brace (`{…}`) on either side, so both
    // delimiters open and close a text node here (code fragments the
    // pattern also happens to catch are inert for these patterns).
    const jsxText = codeOnly
        .join("")
        .matchAll(/[>}]([^<>{}]*[A-Za-z][^<>{}]*)[<{]/g);
    for (const match of jsxText) {
        const text = match[1];
        if (text !== undefined) {
            strings.push(text);
        }
    }
    return strings.filter((text) => text.trim().length > 0);
}

function collectFiles(root: string): string[] {
    const files: string[] = [];
    for (const entry of readdirSync(root)) {
        const path = join(root, entry);
        if (statSync(path).isDirectory()) {
            if (entry === "node_modules" || entry === ".next" || entry === "generated") continue;
            if (entry === "__tests__") continue;
            files.push(...collectFiles(path));
        } else if (EXTENSIONS.some((ext) => path.endsWith(ext))) {
            if (path.includes(".test.") || path.includes(".spec.")) continue;
            files.push(path);
        }
    }
    return files;
}

describe("rendered copy carries no engineering provenance", () => {
    const files = SCAN_ROOTS.flatMap((root) => collectFiles(root));

    it("scans a non-trivial source tree (the gate cannot pass vacuously)", () => {
        expect(files.length).toBeGreaterThan(30);
    });

    it("flags synthetic offenders (the gate itself is falsifiable)", () => {
        expect(BANNED).toHaveLength(Object.keys(OFFENDER_SAMPLES).length);
        for (const { name, regex } of BANNED) {
            const sample = OFFENDER_SAMPLES[name];
            expect(sample).toBeDefined();
            expect(regex.test(sample ?? "")).toBe(true);
        }
    });

    it("extracts strings and JSX text but never comments", () => {
        const sample = [
            "// line comment with issue #31",
            "/* block comment with !52 */",
            'const label = "Visible copy"; // trailing #31 note',
            "const jsx = <p>Rendered text</p>;",
        ].join("\n");
        const extracted = extractRenderedStrings(sample);
        expect(extracted).toContain("Visible copy");
        expect(extracted).toContain("Rendered text");
        expect(extracted.join("\n")).not.toMatch(/#31|!52/);
    });

    it.each(BANNED)("no rendered string contains a $name", ({ regex }) => {
        const offenders: string[] = [];
        for (const path of files) {
            const source = readFileSync(path, "utf8");
            for (const text of extractRenderedStrings(source)) {
                if (regex.test(text)) {
                    offenders.push(`${relative(WEB_ROOT, path).split(sep).join("/")}: ${text.trim().slice(0, 120)}`);
                }
            }
        }
        expect(offenders).toEqual([]);
    });
});
