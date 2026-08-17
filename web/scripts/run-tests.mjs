/**
 * Test entrypoint. CI invokes `npm test -- --ci`; the extra flag is a
 * pipeline convention, not a node:test option, so this wrapper drops
 * unknown args and delegates to the built-in test runner.
 */
import { spawnSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const webRoot = dirname(dirname(fileURLToPath(import.meta.url)));
// Enumerate files explicitly: `node --test <dir>` resolution differs
// between Node 20 (local) and Node 22 (CI); explicit paths behave the
// same everywhere.
const files = readdirSync(join(webRoot, 'tests'))
  .filter((name) => name.endsWith('.test.mjs'))
  .sort()
  .map((name) => join(webRoot, 'tests', name));
if (files.length === 0) {
  console.error('no test files found under web/tests');
  process.exit(1);
}
const result = spawnSync(process.execPath, ['--test', ...files], {
  stdio: 'inherit',
  cwd: webRoot,
});
process.exit(result.status ?? 1);
