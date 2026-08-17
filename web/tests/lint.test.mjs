/**
 * scripts/lint.mjs — the build-time XSS/leakage gate must itself be
 * falsifiable: each rule fires on a minimal offending sample and stays
 * quiet on clean source. Removing a rule breaks these tests.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { checkSource } from '../scripts/lint.mjs';

/** @param {string} text @param {string} file */
function idsFor(text, file = 'src/x.js') {
  return checkSource(file, text).map((f) => f.id);
}

test('each HTML-parser sink is a lint failure', () => {
  assert.ok(idsFor('node.innerHTML = data;').includes('no-innerhtml'));
  assert.ok(idsFor('node.outerHTML = data;').includes('no-outerhtml'));
  assert.ok(idsFor('node.insertAdjacentHTML("beforeend", d);').includes('no-insert-adjacent-html'));
  assert.ok(idsFor('document.write(d);').includes('no-document-write'));
  assert.ok(idsFor('range.createContextualFragment(d);').includes('no-range-fragment'));
});

test('code-execution sinks are lint failures', () => {
  assert.ok(idsFor('eval("x")').includes('no-eval'));
  assert.ok(idsFor('const f = new Function("x")').includes('no-function-ctor'));
  assert.ok(idsFor('a.href = "javascript:alert(1)"').includes('no-js-url'));
});

test('token-custody rules: storage and console are lint failures', () => {
  assert.ok(idsFor('sessionStorage.setItem("t", token)').includes('no-session-storage'));
  assert.ok(idsFor('localStorage.setItem("t", token)').includes('no-local-storage'));
  assert.ok(idsFor('console.log(user.email)').includes('no-console'));
});

test('session.js allowlist covers ONLY localStorage, nothing else', () => {
  assert.deepEqual(idsFor('localStorage.getItem("gp.tenant")', 'src/session.js'), []);
  assert.ok(idsFor('sessionStorage.x', 'src/session.js').includes('no-session-storage'));
  assert.ok(idsFor('n.innerHTML = 1', 'src/session.js').includes('no-innerhtml'));
});

test('clean source passes', () => {
  assert.deepEqual(idsFor('const x = el("div", {}, name); node.textContent = x;'), []);
});

test('line numbers point at the offending line', () => {
  const findings = checkSource('src/x.js', 'const a = 1;\nnode.innerHTML = b;\n');
  assert.equal(findings.length, 1);
  assert.equal(findings[0].line, 2);
});
