/**
 * Smoke: every browser module loads under Node and the lint gate runs
 * clean on the shipped sources. The full adversarial suites (fake-DOM
 * XSS proofs, client contract, storage discipline) land with the tests
 * commit of this MR.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

test('src modules import cleanly', async () => {
  await import('../src/dom.js');
  await import('../src/session.js');
  await import('../src/api.js');
  await import('../src/format.js');
  await import('../src/screens.js');
  await import('../src/main.js'); // boot() no-ops without a shell document
  assert.ok(true);
});

test('lint checkSource passes on shipped sources', async () => {
  const { checkSource } = await import('../scripts/lint.mjs');
  const { readFileSync } = await import('node:fs');
  for (const file of ['src/dom.js', 'src/api.js', 'src/session.js', 'src/format.js', 'src/main.js']) {
    const findings = checkSource(file, readFileSync(new URL(`../${file}`, import.meta.url), 'utf8'));
    assert.deepEqual(findings, [], `${file} should have no lint findings`);
  }
});
