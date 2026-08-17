/**
 * Session state — tokens live in module MEMORY only (threat model 3).
 *
 * The backend session transport (P3) is header-borne: Bearer access
 * token (<= 15 min) + rotating refresh token submitted in POST bodies.
 * There are no auth cookies in this stack, so nothing here may write a
 * token to persistent or session storage — a page reload requires
 * re-login by design. The only persisted value is the tenant id, which
 * is routing configuration, not a credential. scripts/lint.mjs forbids
 * storage APIs outside this file, and the tests assert no token string
 * ever reaches storage.
 */

const TENANT_KEY = 'gp.tenant';

const state = {
  /** @type {string|null} */ accessToken: null,
  /** @type {string|null} */ refreshToken: null,
  /** @type {string|null} */ userId: null,
  /** @type {string|null} */ tenantId: null,
  /** @type {Array<{module:string,can_view:boolean,can_create:boolean,can_edit:boolean,can_approve:boolean}>|null} */
  permissions: null,
};

/**
 * Decode the payload of a JWT WITHOUT verifying it. Used solely to read
 * our own `sub` claim for UX (disabling self-role/self-status buttons);
 * the server is the only verifier and enforcer.
 * @param {string} token
 * @returns {Record<string, unknown>|null}
 */
export function decodeJwtPayload(token) {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4);
    const json = globalThis.atob(padded);
    const parsed = JSON.parse(json);
    return typeof parsed === 'object' && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

/** @param {string} accessToken @param {string} refreshToken */
export function setTokens(accessToken, refreshToken) {
  state.accessToken = accessToken;
  state.refreshToken = refreshToken;
  const payload = decodeJwtPayload(accessToken);
  state.userId = payload && typeof payload.sub === 'string' ? payload.sub : null;
}

export function clearSession() {
  state.accessToken = null;
  state.refreshToken = null;
  state.userId = null;
  state.tenantId = null;
  state.permissions = null;
}

/** Active tenant for this signed-in session (needed by /auth/refresh). */
export function setActiveTenantId(/** @type {string} */ tenantId) {
  state.tenantId = tenantId;
}

export function getActiveTenantId() {
  return state.tenantId;
}

export function getAccessToken() {
  return state.accessToken;
}

export function getRefreshToken() {
  return state.refreshToken;
}

/** The signed-in user's own id (from the unverified `sub` claim; UX only). */
export function getOwnUserId() {
  return state.userId;
}

export function isAuthenticated() {
  return state.accessToken !== null;
}

/** @param {Array<{module:string,can_view:boolean,can_create:boolean,can_edit:boolean,can_approve:boolean}>} perms */
export function setPermissions(perms) {
  state.permissions = perms;
}

/**
 * UI-affordance check ONLY — the server enforces every call (gate 1.6).
 * @param {string} module
 * @param {'view'|'create'|'edit'|'approve'} action
 */
export function can(module, action) {
  if (!state.permissions) return false;
  const row = state.permissions.find((p) => p.module === module);
  if (!row) return false;
  return {
    view: row.can_view,
    create: row.can_create,
    edit: row.can_edit,
    approve: row.can_approve,
  }[action] === true;
}

/**
 * Tenant id persistence — a UUID naming the tenant, not a secret.
 * Guarded: storage may be unavailable (private mode); the app then
 * simply asks again next time.
 * @returns {string}
 */
export function getStoredTenantId() {
  try {
    return globalThis.localStorage?.getItem(TENANT_KEY) ?? '';
  } catch {
    return '';
  }
}

/** @param {string} tenantId */
export function storeTenantId(tenantId) {
  try {
    globalThis.localStorage?.setItem(TENANT_KEY, tenantId);
  } catch {
    /* non-fatal: convenience only */
  }
}
