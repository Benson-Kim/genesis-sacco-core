/**
 * format.js — payload text discipline + keyset pager.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { CursorPager, fmtDateTime, prettyJson, relTime } from '../src/format.js';

test('prettyJson emits exact JSON text; hostile strings survive as data', () => {
  const payload = {
    full_name: '<script>document.location="//evil"</script>',
    branch: '"></td><img src=x onerror=alert(1)>',
    nested: { note: '=HYPERLINK("http://evil")' },
  };
  const text = prettyJson(payload);
  // Hand-computed oracle: JSON.stringify escapes the inner quotes but
  // keeps < > = characters verbatim — they are DATA for a text node.
  assert.ok(text.includes('<script>'));
  assert.ok(text.includes('onerror=alert(1)'));
  assert.deepEqual(JSON.parse(text), payload, 'lossless round-trip');
});

test('prettyJson never throws on unserializable input', () => {
  /** @type {Record<string, unknown>} */
  const cyclic = {};
  cyclic.self = cyclic;
  assert.equal(typeof prettyJson(cyclic), 'string');
  assert.equal(prettyJson(undefined), 'undefined');
});

test('fmtDateTime / relTime fall back to the raw string for garbage', () => {
  // Unparseable input is returned verbatim — it will be RENDERED AS
  // TEXT by dom.js, so a hostile "date" cannot become markup.
  assert.equal(fmtDateTime('<b>not a date</b>'), '<b>not a date</b>');
  assert.equal(relTime('<b>not a date</b>'), '<b>not a date</b>');
  assert.equal(fmtDateTime(null), '—');
  assert.equal(relTime(null), 'never');
});

test('relTime buckets (hand-computed oracles)', () => {
  const now = new Date('2026-07-30T12:00:00Z');
  assert.equal(relTime('2026-07-30T11:59:40Z', now), 'just now');
  assert.equal(relTime('2026-07-30T11:58:00Z', now), '2m ago');
  assert.equal(relTime('2026-07-30T09:00:00Z', now), '3h ago');
  assert.equal(relTime('2026-07-18T12:00:00Z', now), '12d ago');
});

test('CursorPager: cursors only, correct prev/next sequence, no offsets', () => {
  const pager = new CursorPager();
  assert.equal(pager.hasPrev(), false);
  assert.equal(pager.hasNext(), false);
  assert.throws(() => pager.goNext(), /no next page/);

  pager.setNext('c1');
  assert.equal(pager.goNext(), 'c1'); // page 2 requested with c1
  pager.setNext('c2');
  assert.equal(pager.goNext(), 'c2'); // page 3 requested with c2
  pager.setNext(null);
  assert.equal(pager.hasNext(), false, 'terminal page');

  assert.equal(pager.goPrev(), 'c1', 'back to page 2 via stored cursor');
  assert.equal(pager.goPrev(), null, 'back to first page = null cursor');
  assert.equal(pager.hasPrev(), false);
  assert.equal(pager.goPrev(), null, 'prev on first page stays first');

  pager.reset();
  assert.equal(pager.hasPrev(), false);
  assert.equal(pager.hasNext(), false);
});
