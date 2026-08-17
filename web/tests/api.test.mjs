/**
 * api.js — client transport contract.
 *
 * Proves: bearer/idempotency/tenant headers, no credentials in URLs,
 * refresh-once-on-401 (single retry, then session teardown), distinct
 * 401/403/409/422/429 mapping, opaque cursor passthrough. Fetch is
 * mocked; call logs are the oracle.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import {
  ApiError,
  apiFetch,
  buildQuery,
  errorMessage,
  isUuid,
  listAuditLog,
  listUsers,
  toApiError,
  updateUser,
} from '../src/api.js';
import {
  clearSession,
  getAccessToken,
  isAuthenticated,
  setActiveTenantId,
  setTokens,
} from '../src/session.js';

/** base64url-encode a JSON object (JWT payload segment). */
function seg(obj) {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}
const ACCESS = `${seg({ alg: 'HS256' })}.${seg({ sub: '11111111-1111-1111-1111-111111111111' })}.sig`;
const ACCESS2 = `${seg({ alg: 'HS256' })}.${seg({ sub: '11111111-1111-1111-1111-111111111111', v: 2 })}.sig2`;

/** @type {Array<{url:string, method:string, headers:Record<string,string>, body:string|undefined}>} */
let calls;
const realFetch = globalThis.fetch;

/** @param {number} status @param {unknown} body */
function jsonRes(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  };
}

/** @param {(call: {url:string}, n:number) => object} router */
function mockFetch(router) {
  let n = 0;
  // @ts-expect-error test double
  globalThis.fetch = async (url, opts = {}) => {
    const call = {
      url: String(url),
      method: opts.method ?? 'GET',
      headers: opts.headers ?? {},
      body: opts.body,
    };
    calls.push(call);
    n += 1;
    return router(call, n);
  };
}

beforeEach(() => {
  calls = [];
  clearSession();
});
afterEach(() => {
  globalThis.fetch = realFetch;
  clearSession();
});

test('bearer + idempotency + content-type headers; nothing secret in the URL', async () => {
  setTokens(ACCESS, 'refresh-secret-1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  mockFetch(() => jsonRes(200, { ok: true }));
  await apiFetch('/users', { method: 'POST', body: { full_name: 'x' }, idempotencyKey: 'k-1' });
  assert.equal(calls.length, 1);
  const call = calls[0];
  assert.equal(call.headers['authorization'], `Bearer ${ACCESS}`);
  assert.equal(call.headers['idempotency-key'], 'k-1');
  assert.equal(call.headers['content-type'], 'application/json');
  // Threat 3: tokens never in URLs.
  assert.ok(!call.url.includes(ACCESS));
  assert.ok(!call.url.includes('refresh-secret-1'));
});

test('401 -> exactly one refresh -> retry with the ROTATED token', async () => {
  setTokens(ACCESS, 'refresh-secret-1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  mockFetch((call, n) => {
    if (n === 1) return jsonRes(401, { category: 'unauthenticated', correlation_id: 'c1' });
    if (call.url.endsWith('/auth/refresh')) {
      return jsonRes(200, { access_token: ACCESS2, refresh_token: 'refresh-secret-2', expires_in: 900 });
    }
    return jsonRes(200, { items: [], next_cursor: null });
  });
  const page = await apiFetch('/users');
  assert.deepEqual(page, { items: [], next_cursor: null });
  assert.equal(calls.length, 3, 'original + refresh + retry');
  assert.equal(calls[1].url.endsWith('/auth/refresh'), true);
  // Refresh carries the tenant header and the OLD refresh token in the BODY.
  assert.equal(calls[1].headers['x-tenant-id'], '22222222-2222-2222-2222-222222222222');
  assert.equal(JSON.parse(String(calls[1].body)).refresh_token, 'refresh-secret-1');
  assert.ok(!calls[1].url.includes('refresh-secret-1'), 'refresh token never in URL');
  // Retry uses the rotated access token.
  assert.equal(calls[2].headers['authorization'], `Bearer ${ACCESS2}`);
  assert.equal(getAccessToken(), ACCESS2);
});

test('failed refresh tears the session down and surfaces 401', async () => {
  setTokens(ACCESS, 'refresh-secret-1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  mockFetch((call) => {
    if (call.url.endsWith('/auth/refresh')) {
      return jsonRes(401, { category: 'unauthenticated', correlation_id: 'c2' });
    }
    return jsonRes(401, { category: 'unauthenticated', correlation_id: 'c1' });
  });
  await assert.rejects(apiFetch('/users'), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 401);
    return true;
  });
  assert.equal(isAuthenticated(), false, 'session must be cleared');
  assert.equal(calls.length, 2, 'no endless retry loop');
});

test('409 maps to a distinct conflict error with the correlation id', async () => {
  setTokens(ACCESS, 'r1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  mockFetch(() => jsonRes(409, { category: 'conflict', correlation_id: 'corr-409' }));
  await assert.rejects(
    updateUser('u1', { version: 1, full_name: 'x' }, 'k-2'),
    (err) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, 409);
      assert.equal(err.category, 'conflict');
      assert.equal(err.correlationId, 'corr-409');
      // The stale-edit contract: the client raises; it must NOT have
      // auto-retried with a different version.
      assert.equal(calls.length, 1);
      return true;
    },
  );
});

test('422 FastAPI detail parses into field errors without trusting shapes', () => {
  const err = toApiError(422, {
    detail: [
      { loc: ['body', 'email'], msg: 'value is not a valid email address', type: 'value_error' },
      'garbage-entry',
      { loc: 'not-an-array', msg: 42 },
    ],
  }, 'hdr-cid');
  assert.equal(err.status, 422);
  assert.equal(err.fields.length, 2);
  assert.deepEqual(err.fields[0], { field: 'email', message: 'value is not a valid email address' });
  assert.equal(err.correlationId, 'hdr-cid');
});

test('error messages are least-disclosure per status', () => {
  const cases = [
    [401, 'Session expired — sign in again.'],
    [403, 'Not permitted.'],
    [409, 'The record changed or conflicts with existing data — reload before retrying.'],
    [422, 'Validation failed — check the highlighted fields.'],
    [429, 'Too many attempts — wait a minute and retry.'],
  ];
  for (const [status, expected] of cases) {
    const { title } = errorMessage(new ApiError({ status: Number(status), category: 'x', correlationId: '' }));
    assert.equal(title, expected, `status ${status}`);
  }
  // Unknown errors: generic text, no internals echoed.
  const { title } = errorMessage(new Error('secret internal detail'));
  assert.ok(!title.includes('secret internal detail'));
});

test('buildQuery: drops empties, encodes values, keys are code-owned', () => {
  assert.equal(buildQuery({ a: '1', b: null, c: undefined, d: '' }), '?a=1');
  assert.equal(buildQuery({ cursor: '2026-01-01T00:00:00+00:00|42' }), '?cursor=2026-01-01T00%3A00%3A00%2B00%3A00%7C42');
  assert.equal(buildQuery({}), '');
});

test('list endpoints pass opaque cursors through untouched', async () => {
  setTokens(ACCESS, 'r1');
  setActiveTenantId('22222222-2222-2222-2222-222222222222');
  mockFetch(() => jsonRes(200, { items: [], next_cursor: null }));
  const cursor = '2026-07-01T10:00:00+00:00|999';
  await listAuditLog({ cursor, entity: 'users', action: 'user.role' });
  await listUsers({ cursor, status: 'active' });
  const q1 = new URL(`http://x${calls[0].url}`).searchParams;
  assert.equal(q1.get('cursor'), cursor, 'cursor round-trips byte-identical');
  assert.equal(q1.get('entity'), 'users');
  const q2 = new URL(`http://x${calls[1].url}`).searchParams;
  assert.equal(q2.get('cursor'), cursor);
  assert.equal(q2.get('status'), 'active');
});

test('isUuid accepts canonical UUIDs and rejects injection shapes', () => {
  assert.ok(isUuid('11111111-1111-1111-1111-111111111111'));
  assert.ok(!isUuid('11111111-1111-1111-1111-11111111111Z'));
  assert.ok(!isUuid("' OR 1=1 --"));
  assert.ok(!isUuid(''));
});
