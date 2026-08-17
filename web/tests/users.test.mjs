/**
 * users.js — screen-level adversarial tests through the REAL screen
 * code on the fake DOM (whose HTML-parser sinks throw).
 *
 * Covers: hostile names stay inert text; stale edit (409) shows the
 * reload flow and NEVER auto-retries; permission-gated affordances;
 * self-row separation-of-duties affordance; idempotency-key stability.
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
import { idempotencyKeyFor, mountUsers } from '../src/users.js';

const HOSTILE_NAME = '<img src=x onerror=alert(document.cookie)>';
const USER_ID = '55555555-5555-5555-5555-555555555555';
const ROLE_ID = '66666666-6666-6666-6666-666666666666';

function seg(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}
/** @param {string} sub */
function tokenFor(sub) {
  return `${seg({ alg: 'HS256' })}.${seg({ sub })}.sig`;
}

const baseUser = () => ({
  id: USER_ID,
  full_name: HOSTILE_NAME,
  email: 'hostile@sacco.co.ke',
  phone: null,
  branch: '"?><script>x</script>',
  role_id: ROLE_ID,
  role_name: 'Teller',
  status: 'active',
  last_active_at: null,
  version: 3,
});

/** @type {() => void} */
let restoreDom;
/** @type {Array<{url:string, method:string, body:string|undefined}>} */
let calls;
const realFetch = globalThis.fetch;

function jsonRes(status, body) {
  return { ok: status >= 200 && status < 300, status, headers: { get: () => null }, json: async () => body };
}

/**
 * @param {{putStatus?: number, user?: Record<string, unknown>}} [opts]
 */
function route(opts = {}) {
  const user = opts.user ?? baseUser();
  // @ts-expect-error test double
  globalThis.fetch = async (url, init = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ url: String(url), method, body: init.body });
    const path = String(url);
    if (path.startsWith('/access/roles')) {
      return jsonRes(200, [
        { id: ROLE_ID, name: 'Teller', is_system: true },
        { id: '77777777-7777-7777-7777-777777777777', name: 'System Admin', is_system: true },
      ]);
    }
    if (method === 'PUT') {
      return jsonRes(opts.putStatus ?? 200, opts.putStatus === 409
        ? { category: 'conflict', correlation_id: 'corr-stale' }
        : { ...user, version: Number(user.version) + 1 });
    }
    if (path.startsWith(`/users/${USER_ID}`)) return jsonRes(200, user);
    if (path.startsWith('/users')) return jsonRes(200, { items: [user], next_cursor: null });
    return jsonRes(404, { category: 'not_found', correlation_id: 'x' });
  };
}

function mount() {
  const container = new FakeElement('main');
  const modalHost = new FakeElement('div');
  let expired = 0;
  mountUsers(container, { onSessionExpired: () => { expired += 1; }, modalHost });
  return { container, modalHost, expiredCount: () => expired };
}

beforeEach(() => {
  restoreDom = installDom();
  calls = [];
  clearSession();
  setTokens(tokenFor('99999999-9999-9999-9999-999999999999'), 'r1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  setPermissions([
    { module: 'access_control', can_view: true, can_create: true, can_edit: true, can_approve: true },
  ]);
});
afterEach(() => {
  restoreDom();
  globalThis.fetch = realFetch;
  clearSession();
});

test('hostile user fields render as inert text nodes through the real screen', async () => {
  route();
  const { container } = mount();
  await flush();
  const texts = allTextData(container);
  assert.ok(texts.includes(HOSTILE_NAME), 'name present byte-identical as TEXT data');
  assert.ok(texts.includes('"?><script>x</script>'), 'branch present as TEXT data');
  // The fake DOM throws on parser sinks, so reaching this point proves
  // no markup interpretation happened anywhere in the render path.
});

test('stale edit: 409 shows the reload flow, never auto-retries, reload refreshes version', async () => {
  route({ putStatus: 409 });
  const { container, modalHost } = mount();
  await flush();

  const row = findAll(container, (n) => n.tagName === 'TR' && (n.attrs.class ?? '').includes('rowbtn'))[0];
  await Promise.all(row.dispatch('click'));
  await flush();

  await Promise.all(buttonByText(modalHost, 'Edit profile').dispatch('click'));
  await flush();
  await Promise.all(buttonByText(modalHost, 'Save changes').dispatch('click'));
  await flush();

  const putCalls = calls.filter((c) => c.method === 'PUT');
  assert.equal(putCalls.length, 1, 'exactly ONE write attempt — no silent retry with a fresher version');
  const text = allTextData(modalHost).join(' ');
  assert.ok(text.includes('Your change was NOT applied'), 'conflict banner shown');

  // Version sent was the loaded one (3) — hand-computed oracle.
  assert.equal(JSON.parse(String(putCalls[0].body)).version, 3);

  // Explicit reload flow refreshes the record (server now at version 9).
  route({ user: { ...baseUser(), version: 9 } });
  await Promise.all(buttonByText(modalHost, 'Reload record').dispatch('click'));
  await flush();
  assert.ok(allTextData(modalHost).join(' ').includes('record version 9'), 'form rebound to the fresh version');
});

test('UI affordances follow the matrix: view-only role sees no mutation controls', async () => {
  setPermissions([
    { module: 'access_control', can_view: true, can_create: false, can_edit: false, can_approve: false },
  ]);
  route();
  const { container, modalHost } = mount();
  await flush();
  assert.equal(buttonByText(container, '+ Add user'), null, 'create affordance hidden');
  const row = findAll(container, (n) => n.tagName === 'TR' && (n.attrs.class ?? '').includes('rowbtn'))[0];
  await Promise.all(row.dispatch('click'));
  await flush();
  for (const label of ['Edit profile', 'Change role', 'Suspend', 'Invalidate pending OTP', 'Force OTP re-enrolment']) {
    assert.equal(buttonByText(modalHost, label), null, `${label} hidden for view-only role`);
  }
});

test('own row: role/status affordances disabled (separation of duties UX)', async () => {
  // Sign in AS the listed user.
  setTokens(tokenFor(USER_ID), 'r1');
  route();
  const { container, modalHost } = mount();
  await flush();
  const row = findAll(container, (n) => n.tagName === 'TR' && (n.attrs.class ?? '').includes('rowbtn'))[0];
  await Promise.all(row.dispatch('click'));
  await flush();
  assert.equal(buttonByText(modalHost, 'Suspend').disabled, true);
  assert.equal(buttonByText(modalHost, 'Change role').disabled, true);
  // Server remains the enforcer; this is UX. Profile edit stays allowed.
  assert.equal(buttonByText(modalHost, 'Edit profile').disabled, false);
});

test('401 during load hands off to the session-expired flow', async () => {
  // @ts-expect-error test double
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method ?? 'GET', body: init.body });
    return jsonRes(401, { category: 'unauthenticated', correlation_id: 'c' });
  };
  const { expiredCount } = mount();
  await flush();
  assert.ok(expiredCount() >= 1, 'shell notified to re-login');
});

test('idempotency keys: stable for identical bodies, rotated when the body changes', () => {
  let n = 0;
  const fresh = () => `key-${(n += 1)}`;
  const slot = { key: null, body: null };
  const k1 = idempotencyKeyFor(slot, '{"op":"update","v":1}', fresh);
  const k2 = idempotencyKeyFor(slot, '{"op":"update","v":1}', fresh);
  assert.equal(k1, k2, 'same logical submission -> same key (middleware replays)');
  const k3 = idempotencyKeyFor(slot, '{"op":"update","v":2}', fresh);
  assert.notEqual(k3, k1, 'changed body -> new key (new logical submission)');
});
