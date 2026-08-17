/**
 * session.js — token custody discipline (threat model 3).
 *
 * Falsifiable oracle: a recording fake storage captures every write.
 * If session handling ever persists a token (the regression this
 * guards), the assertions below fail.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  can,
  clearSession,
  decodeJwtPayload,
  getAccessToken,
  getOwnUserId,
  getStoredTenantId,
  setPermissions,
  setTokens,
  storeTenantId,
} from '../src/session.js';

function seg(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

const SUB = '33333333-3333-3333-3333-333333333333';
const ACCESS = `${seg({ alg: 'HS256' })}.${seg({ sub: SUB, tid: 't', rid: 'r' })}.signature`;

/** @type {Record<string, string>} */
let written;
const realStorage = globalThis.localStorage;

beforeEach(() => {
  written = {};
  // @ts-expect-error test double
  globalThis.localStorage = {
    getItem: (k) => written[k] ?? null,
    setItem: (k, v) => {
      written[k] = String(v);
    },
  };
  clearSession();
});
afterEach(() => {
  // @ts-expect-error test double
  globalThis.localStorage = realStorage;
  clearSession();
});

test('tokens NEVER reach storage; only the non-secret tenant id persists', () => {
  setTokens(ACCESS, 'refresh-token-secret');
  storeTenantId('44444444-4444-4444-4444-444444444444');
  assert.deepEqual(Object.keys(written), ['gp.tenant'], 'exactly one persisted key');
  const dump = JSON.stringify(written);
  assert.ok(!dump.includes(ACCESS), 'access token must not be persisted');
  assert.ok(!dump.includes('refresh-token-secret'), 'refresh token must not be persisted');
  assert.equal(getStoredTenantId(), '44444444-4444-4444-4444-444444444444');
});

test('clearSession drops every credential', () => {
  setTokens(ACCESS, 'r');
  clearSession();
  assert.equal(getAccessToken(), null);
  assert.equal(getOwnUserId(), null);
});

test('decodeJwtPayload reads sub for UX and rejects garbage safely', () => {
  setTokens(ACCESS, 'r');
  assert.equal(getOwnUserId(), SUB);
  assert.equal(decodeJwtPayload('not-a-jwt'), null);
  assert.equal(decodeJwtPayload('a.b'), null);
  assert.equal(decodeJwtPayload(`${seg({})}.%%%%.c`), null);
});

test('can() is deny-by-default UX gating', () => {
  assert.equal(can('access_control', 'view'), false, 'no permissions loaded -> deny');
  setPermissions([
    { module: 'access_control', can_view: true, can_create: false, can_edit: true, can_approve: false },
  ]);
  assert.equal(can('access_control', 'view'), true);
  assert.equal(can('access_control', 'create'), false);
  assert.equal(can('access_control', 'edit'), true);
  assert.equal(can('members', 'view'), false, 'unknown module -> deny');
  // @ts-expect-error adversarial action name
  assert.equal(can('access_control', 'delete'), false, 'unknown action -> deny');
});
