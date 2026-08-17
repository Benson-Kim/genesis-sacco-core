/**
 * Security lint for the static admin web (merge blocker, gate 1.6).
 *
 * The P13.5 threat model names XSS-via-audit-payload as a primary
 * threat: audit before/after JSON is attacker-influenced data. The
 * structural defence is that no source file may hand markup to the
 * HTML parser or hold tokens in persistent storage. This lint makes
 * those defences build failures, so removing one fails CI
 * (falsifiability — tests/lint.test.mjs proves each rule fires).
 *
 * Also import-checks every src module under Node so a syntax error or
 * a bad static import fails the lint stage, not the browser.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, extname, join, relative } from 'node:path';

const webRoot = dirname(fileURLToPath(new URL('.', import.meta.url)));

/** Rules applied to every checked file unless the file is allowlisted. */
const RULES = [
  // HTML-parser sinks: the one structural XSS defence of this app.
  { id: 'no-innerhtml', re: /\binnerHTML\b/, why: 'HTML-parser sink; use src/dom.js text-node construction' },
  { id: 'no-outerhtml', re: /\bouterHTML\b/, why: 'HTML-parser sink' },
  { id: 'no-insert-adjacent-html', re: /\binsertAdjacentHTML\b/, why: 'HTML-parser sink' },
  { id: 'no-document-write', re: /\bdocument\s*\.\s*write(ln)?\b/, why: 'HTML-parser sink' },
  { id: 'no-range-fragment', re: /\bcreateContextualFragment\b/, why: 'HTML-parser sink' },
  // Code-execution sinks.
  { id: 'no-eval', re: /\beval\s*\(/, why: 'code execution sink' },
  { id: 'no-function-ctor', re: /\bnew\s+Function\s*\(/, why: 'code execution sink' },
  { id: 'no-js-url', re: /javascript\s*:/i, why: 'javascript: URL vector' },
  // Token / data leakage.
  { id: 'no-session-storage', re: /\bsessionStorage\b/, why: 'tokens live in memory only (threat model 3)' },
  {
    id: 'no-local-storage',
    re: /\blocalStorage\b/,
    why: 'only session.js may persist (non-secret tenant id only)',
    allow: ['src/session.js'],
  },
  {
    id: 'no-console',
    re: /\bconsole\s*\.\s*(log|info|debug|warn|error|trace|dir)\b/,
    why: 'no API data in the console (threat model 3)',
    allow: ['scripts/lint.mjs', 'scripts/run-tests.mjs', 'scripts/build.mjs'],
  },
];

/** Extra rules for index.html: everything must go through src modules. */
const HTML_RULES = [
  { id: 'no-inline-handler', re: /\son[a-z]+\s*=/i, why: 'inline event handlers are unauditable sinks' },
  { id: 'no-inline-script', re: /<script(?![^>]*\bsrc=)[^>]*>/i, why: 'inline scripts break CSP script-src self' },
];

/** Recursively collect files with the given extensions. */
function collect(dir, exts, out = []) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name === 'node_modules' || name === 'dist') continue;
      collect(path, exts, out);
    } else if (exts.includes(extname(name))) {
      out.push(path);
    }
  }
  return out;
}

/**
 * Pure rule check so the test suite can prove each rule fires.
 * @param {string} rel - path relative to web/, using forward slashes
 * @param {string} text - file contents
 * @param {{id:string,re:RegExp,why:string,allow?:string[]}[]} rules
 * @returns {{file:string,id:string,line:number,why:string}[]}
 */
export function checkSource(rel, text, rules = RULES) {
  const findings = [];
  for (const rule of rules) {
    if (rule.allow && rule.allow.includes(rel)) continue;
    const lines = text.split('\n');
    for (let i = 0; i < lines.length; i += 1) {
      if (rule.re.test(lines[i])) {
        findings.push({ file: rel, id: rule.id, line: i + 1, why: rule.why });
      }
    }
  }
  return findings;
}

async function main() {
  // The rules protect what the BROWSER loads: src/ modules and
  // index.html. Node-side tooling (scripts/, tests/) legitimately
  // names the forbidden patterns as data (rule definitions, fixtures).
  const findings = [];
  const jsFiles = collect(join(webRoot, 'src'), ['.js', '.mjs']);
  for (const path of jsFiles) {
    const rel = relative(webRoot, path).split('\\').join('/');
    findings.push(...checkSource(rel, readFileSync(path, 'utf8')));
  }
  const htmlPath = join(webRoot, 'index.html');
  const html = readFileSync(htmlPath, 'utf8');
  findings.push(...checkSource('index.html', html, [...RULES, ...HTML_RULES]));

  // Import-check src modules (syntax + top-level integrity under Node).
  for (const path of collect(join(webRoot, 'src'), ['.js'])) {
    try {
      await import(pathToFileURL(path).href);
    } catch (err) {
      findings.push({
        file: relative(webRoot, path),
        id: 'import-check',
        line: 0,
        why: err instanceof Error ? err.message : String(err),
      });
    }
  }

  if (findings.length > 0) {
    for (const f of findings) {
      console.error(`LINT ${f.file}:${f.line} [${f.id}] ${f.why}`);
    }
    process.exit(1);
  }
  console.error(`lint ok: ${jsFiles.length + 1} files checked`);
}

// Run only as a CLI, not when imported by the test suite.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
