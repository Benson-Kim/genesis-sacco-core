/**
 * API client layer (P13.5 frontend).
 *
 * - Transport follows the existing P3 pattern exactly: Bearer access
 *   token in the Authorization header, refresh token in POST bodies,
 *   `x-tenant-id` header on pre-auth endpoints. No cookies — so a
 *   cross-site page cannot attach our credentials (CSRF, threat 2).
 * - Credentials and PII never enter URLs: auth travels in headers and
 *   bodies only (threat 3; asserted by tests).
 * - Every mutation carries an `Idempotency-Key` supplied by the caller
 *   and kept stable across retries of the same logical submission
 *   (gate 1.4).
 * - On 401 the client attempts ONE silent refresh (single-flight),
 *   retries the original request once, and otherwise surfaces a
 *   distinct session-expired error so the shell can return to the
 *   sign-in gate (threat model 5: error messages stay least-disclosure;
 *   the envelope category and correlation id are all we render).
 */

import {
  clearSession,
  getAccessToken,
  getActiveTenantId,
  getRefreshToken,
  setTokens,
} from './session.js';

/** Deployment override; defaults to same-origin. Never carries secrets. */
function apiBase() {
  const v = /** @type {Record<string, unknown>} */ (globalThis)['GENESIS_API_BASE'];
  return typeof v === 'string' ? v : '';
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** @param {string} value */
export function isUuid(value) {
  return UUID_RE.test(value);
}

/** Fresh idempotency key for a NEW logical submission. */
export function newIdempotencyKey() {
  return globalThis.crypto.randomUUID();
}

/**
 * Build a query string from defined, non-empty params only. Values are
 * encoded by URLSearchParams; keys are code-owned literals.
 * @param {Record<string, string|number|null|undefined>} params
 */
export function buildQuery(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    search.set(key, String(value));
  }
  const s = search.toString();
  return s ? `?${s}` : '';
}

export class ApiError extends Error {
  /**
   * @param {{status:number, category:string, correlationId:string,
   *          fields?: Array<{field:string, message:string}>}} info
   */
  constructor(info) {
    // Message is a category, never server internals or payload data.
    super(`api error ${info.status} (${info.category})`);
    this.name = 'ApiError';
    this.status = info.status;
    this.category = info.category;
    this.correlationId = info.correlationId;
    this.fields = info.fields ?? [];
  }
}

/**
 * Least-disclosure operator-facing message for an error (threat 5:
 * no capability probing hints, no server internals, no data echo).
 * @param {unknown} err
 * @returns {{title:string, correlationId:string}}
 */
export function errorMessage(err) {
  if (!(err instanceof ApiError)) {
    return { title: 'Network error — check connectivity and retry.', correlationId: '' };
  }
  const titles = {
    400: 'Validation failed — check the highlighted fields.',
    401: 'Session expired — sign in again.',
    403: 'Not permitted.',
    404: 'Not found.',
    409: 'The record changed or conflicts with existing data — reload before retrying.',
    422: 'Validation failed — check the highlighted fields.',
    429: 'Too many attempts — wait a minute and retry.',
  };
  return {
    title: /** @type {Record<number,string>} */ (titles)[err.status] ?? 'Something went wrong — retry or contact support.',
    correlationId: err.correlationId,
  };
}

/**
 * Parse an error response body into an ApiError without ever trusting
 * its shape. Handles both the app envelope {category, correlation_id}
 * and FastAPI validation bodies {detail: [{loc, msg}]}.
 * @param {number} status
 * @param {unknown} body
 * @param {string} headerCorrelationId
 */
export function toApiError(status, body, headerCorrelationId) {
  let category = 'unknown';
  let correlationId = headerCorrelationId;
  /** @type {Array<{field:string, message:string}>} */
  const fields = [];
  if (typeof body === 'object' && body !== null) {
    const rec = /** @type {Record<string, unknown>} */ (body);
    if (typeof rec.category === 'string') category = rec.category;
    if (typeof rec.correlation_id === 'string' && rec.correlation_id) {
      correlationId = rec.correlation_id;
    }
    if (Array.isArray(rec.detail)) {
      category = category === 'unknown' ? 'validation_error' : category;
      for (const item of rec.detail) {
        if (typeof item !== 'object' || item === null) continue;
        const d = /** @type {Record<string, unknown>} */ (item);
        const loc = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : '';
        const msg = typeof d.msg === 'string' ? d.msg : 'invalid value';
        fields.push({ field: loc, message: msg });
      }
    }
  }
  return new ApiError({ status, category, correlationId, fields });
}

/** Single-flight refresh so parallel 401s trigger one rotation. */
let refreshInflight = /** @type {Promise<boolean>|null} */ (null);

async function refreshOnce() {
  if (!refreshInflight) {
    refreshInflight = (async () => {
      const refreshToken = getRefreshToken();
      const tenantId = getActiveTenantId();
      if (!refreshToken || !tenantId) return false;
      try {
        const res = await globalThis.fetch(`${apiBase()}/auth/refresh`, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            'x-tenant-id': tenantId,
          },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!res.ok) return false;
        const data = await res.json();
        if (typeof data?.access_token !== 'string' || typeof data?.refresh_token !== 'string') {
          return false;
        }
        setTokens(data.access_token, data.refresh_token);
        return true;
      } catch {
        return false;
      } finally {
        // allow the next expiry to refresh again
        setTimeout(() => {
          refreshInflight = null;
        }, 0);
      }
    })();
  }
  return refreshInflight;
}

/**
 * Perform an API request.
 * @param {string} path - code-owned path; ids must be pre-encoded by callers
 * @param {{method?: string, body?: unknown, idempotencyKey?: string,
 *          tenantId?: string, auth?: boolean, retried?: boolean}} [opts]
 * @returns {Promise<unknown>} parsed JSON (or null for 204)
 */
export async function apiFetch(path, opts = {}) {
  const { method = 'GET', body, idempotencyKey, tenantId, auth = true, retried = false } = opts;
  /** @type {Record<string, string>} */
  const headers = {};
  if (body !== undefined) headers['content-type'] = 'application/json';
  if (idempotencyKey) headers['idempotency-key'] = idempotencyKey;
  if (tenantId) headers['x-tenant-id'] = tenantId;
  if (auth) {
    const token = getAccessToken();
    if (token) headers['authorization'] = `Bearer ${token}`;
  }
  const res = await globalThis.fetch(`${apiBase()}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && auth) {
    if (!retried && (await refreshOnce())) {
      return apiFetch(path, { ...opts, retried: true });
    }
    // Refresh failed or the retry itself came back 401: the session is
    // over — drop tokens so the shell returns to the sign-in gate.
    clearSession();
  }
  if (!res.ok) {
    let parsed = null;
    try {
      parsed = await res.json();
    } catch {
      parsed = null;
    }
    throw toApiError(res.status, parsed, res.headers.get('x-request-id') ?? '');
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ------------------------- auth endpoints ------------------------- */

/** @param {string} tenantId @param {string} email */
export function requestOtp(tenantId, email) {
  return apiFetch('/auth/otp/request', {
    method: 'POST',
    body: { email },
    tenantId,
    auth: false,
  });
}

/** @param {string} tenantId @param {string} email @param {string} code */
export async function verifyOtp(tenantId, email, code) {
  const data = /** @type {Record<string, unknown>} */ (
    await apiFetch('/auth/otp/verify', {
      method: 'POST',
      body: { email, code },
      tenantId,
      auth: false,
    })
  );
  return data;
}

/** Best-effort server-side revocation; local state is cleared regardless. */
export async function logout() {
  const refreshToken = getRefreshToken();
  const tenantId = getActiveTenantId();
  if (refreshToken && tenantId) {
    try {
      await apiFetch('/auth/logout', {
        method: 'POST',
        body: { refresh_token: refreshToken },
        tenantId,
        auth: false,
      });
    } catch {
      /* revocation failure must not trap the user in a session */
    }
  }
  clearSession();
}

/* ----------------------- users administration --------------------- */

/** @param {{cursor?:string|null, limit?:number, status?:string, role_id?:string}} params */
export function listUsers(params) {
  return apiFetch(`/users${buildQuery({
    cursor: params.cursor ?? null,
    limit: params.limit ?? 20,
    status: params.status ?? null,
    role_id: params.role_id ?? null,
  })}`);
}

/** @param {string} userId */
export function getUser(userId) {
  return apiFetch(`/users/${encodeURIComponent(userId)}`);
}

/** @param {Record<string, unknown>} body @param {string} key */
export function createUser(body, key) {
  return apiFetch('/users', { method: 'POST', body, idempotencyKey: key });
}

/** @param {string} userId @param {Record<string, unknown>} body @param {string} key */
export function updateUser(userId, body, key) {
  return apiFetch(`/users/${encodeURIComponent(userId)}`, {
    method: 'PUT',
    body,
    idempotencyKey: key,
  });
}

/** @param {string} userId @param {{version:number, status:string}} body @param {string} key */
export function changeUserStatus(userId, body, key) {
  return apiFetch(`/users/${encodeURIComponent(userId)}/status`, {
    method: 'POST',
    body,
    idempotencyKey: key,
  });
}

/** @param {string} userId @param {{version:number, role_id:string}} body @param {string} key */
export function assignUserRole(userId, body, key) {
  return apiFetch(`/users/${encodeURIComponent(userId)}/role`, {
    method: 'POST',
    body,
    idempotencyKey: key,
  });
}

/** @param {string} userId @param {string} key */
export function invalidateUserOtp(userId, key) {
  return apiFetch(`/users/${encodeURIComponent(userId)}/otp/invalidate`, {
    method: 'POST',
    idempotencyKey: key,
  });
}

/** @param {string} userId @param {string} key */
export function reenrolUserOtp(userId, key) {
  return apiFetch(`/users/${encodeURIComponent(userId)}/otp/reenrol`, {
    method: 'POST',
    idempotencyKey: key,
  });
}

/* --------------------------- rbac lookups ------------------------- */

export function listRoles() {
  return apiFetch('/access/roles');
}

export function myPermissions() {
  return apiFetch('/me/permissions');
}

/* --------------------------- audit log ---------------------------- */

/**
 * @param {{cursor?:string|null, limit?:number, entity?:string, actor_id?:string,
 *          action?:string, date_from?:string, date_to?:string}} params
 */
export function listAuditLog(params) {
  return apiFetch(`/audit-log${buildQuery({
    cursor: params.cursor ?? null,
    limit: params.limit ?? 20,
    entity: params.entity ?? null,
    actor_id: params.actor_id ?? null,
    action: params.action ?? null,
    date_from: params.date_from ?? null,
    date_to: params.date_to ?? null,
  })}`);
}
