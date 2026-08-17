/**
 * dom.js — the structural XSS defence (threat model 1).
 *
 * Oracles are hand-written hostile strings; the fake DOM throws on any
 * HTML-parser sink, so these tests FAIL if dom.js (or a caller) ever
 * switches to innerHTML-style construction (falsifiability, §4).
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { installDom, FakeTextNode } from './helpers/fakedom.mjs';

/** @type {() => void} */
let restore;
beforeEach(() => {
  restore = installDom();
});
afterEach(() => restore());

const HOSTILE = '<img src=x onerror=alert(document.cookie)>"</td><script>steal()</script>';

test('string children become text nodes; hostile markup stays character data', async () => {
  const { el } = await import('../src/dom.js');
  const node = el('td', {}, HOSTILE);
  assert.equal(node.children.length, 1);
  assert.ok(node.children[0] instanceof FakeTextNode, 'child must be a TEXT node');
  // Byte-for-byte the attacker string — never parsed, never mutated.
  assert.equal(node.children[0].data, HOSTILE);
});

test('nested construction keeps hostile data inert at any depth', async () => {
  const { el } = await import('../src/dom.js');
  const tree = el('div', {}, [el('span', { class: 'x' }, [HOSTILE, el('b', {}, HOSTILE)])]);
  assert.equal(tree.textContent, HOSTILE + HOSTILE);
});

test('attributes are allowlisted: event/URL attributes are rejected', async () => {
  const { el } = await import('../src/dom.js');
  // onclick-as-string is a code sink; href/src are URL sinks. All must throw.
  assert.throws(() => el('button', { onclick: 'alert(1)' }), /not allowlisted/);
  assert.throws(() => el('a', { href: 'javascript-scheme-would-go-here' }), /not allowlisted/);
  assert.throws(() => el('img', { src: 'x' }), /not allowlisted/);
  assert.throws(() => el('div', { style: 'color:red' }), /not allowlisted/);
});

test('event handlers attach as functions via addEventListener only', async () => {
  const { el } = await import('../src/dom.js');
  let clicked = 0;
  const btn = el('button', { on: { click: () => { clicked += 1; } } }, 'Go');
  btn.dispatch('click');
  assert.equal(clicked, 1);
});

test('setText/replace/clear never touch parser sinks', async () => {
  const { el, setText, replace, clear } = await import('../src/dom.js');
  const node = el('div', {}, 'seed');
  setText(node, HOSTILE);
  assert.equal(node.textContent, HOSTILE);
  replace(node, [HOSTILE, HOSTILE]);
  assert.equal(node.children.length, 2);
  clear(node);
  assert.equal(node.children.length, 0);
});

test('guard is falsifiable: the fake DOM throws on innerHTML use', () => {
  const doc = globalThis.document;
  const node = doc.createElement('div');
  assert.throws(() => {
    node.innerHTML = HOSTILE;
  }, /FORBIDDEN HTML-parser sink/);
});
