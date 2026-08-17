/**
 * Shell: frame guard, OTP sign-in gate, permission-gated navigation.
 *
 * The gate mirrors the P3 auth contract (tenant header + email + OTP).
 * Deliberate differences from the prototype's demo gate (recorded in
 * GAP_ANALYSIS §2.1 as demo artifacts, not requirements):
 *  - no role picker: the role comes from the user record server-side;
 *  - the OTP code is NEVER displayed — it arrives out-of-band.
 * UI affordances are gated by GET /me/permissions as UX only; the
 * server enforces every call (gate 1.6).
 */

import { el, replace, setText } from './dom.js';
import {
  errorMessage,
  logout,
  myPermissions,
  requestOtp,
  verifyOtp,
  isUuid,
} from './api.js';
import {
  can,
  clearSession,
  getStoredTenantId,
  isAuthenticated,
  setActiveTenantId,
  setPermissions,
  setTokens,
  storeTenantId,
} from './session.js';
import { SCREENS } from './screens.js';

const state = {
  stage: /** @type {'signin'|'otp'} */ ('signin'),
  tenantId: '',
  email: '',
  /** transient operator notice shown on the gate (e.g. session expired) */
  gateNotice: '',
  activeScreen: /** @type {string|null} */ (null),
  busy: false,
};

/** @param {string} id */
function byId(id) {
  const node = globalThis.document.getElementById(id);
  if (!node) throw new Error(`missing shell element #${id}`);
  return node;
}

/* ----------------------------- gate ------------------------------- */

function gateHead() {
  return el('div', { class: 'gatehead' }, [
    el('div', { class: 'mark' }, 'G'),
    el('div', {}, [
      el('div', { class: 'gn' }, 'Genesis Prestige'),
      el('div', { class: 'gt' }, 'ZURI GENESIS · SACCO'),
    ]),
  ]);
}

/**
 * @param {unknown} err
 * @returns {HTMLElement}
 */
function errorBanner(err) {
  const { title, correlationId } = errorMessage(err);
  return el('div', { class: 'banner error mt12' }, [
    title,
    correlationId ? el('span', { class: 'cid' }, `ref: ${correlationId}`) : null,
  ]);
}

function renderGate() {
  const gate = byId('gate');
  gate.hidden = false;
  byId('app').hidden = true;
  const errHost = el('div', {});
  if (state.stage === 'otp') {
    replace(gate, [gateOtpCard(errHost)]);
  } else {
    replace(gate, [gateSigninCard(errHost)]);
  }
}

/** @param {HTMLElement} errHost */
function gateSigninCard(errHost) {
  const tenantInput = el('input', {
    id: 'lg_tenant',
    placeholder: '00000000-0000-0000-0000-000000000000',
    value: state.tenantId || getStoredTenantId(),
    autocomplete: 'off',
  });
  const emailInput = el('input', {
    id: 'lg_email',
    type: 'email',
    placeholder: 'you@sacco.co.ke',
    value: state.email,
    autocomplete: 'email',
  });
  const submit = el('button', { class: 'btn p w100 mt8', on: { click: onSendOtp } }, 'Send OTP');

  async function onSendOtp() {
    if (state.busy) return;
    const tenantId = /** @type {HTMLInputElement} */ (tenantInput).value.trim();
    const email = /** @type {HTMLInputElement} */ (emailInput).value.trim();
    replace(errHost, []);
    if (!isUuid(tenantId)) {
      replace(errHost, [el('div', { class: 'banner error mt12' }, 'Enter a valid tenant id (UUID).')]);
      return;
    }
    if (email.length < 3) {
      replace(errHost, [el('div', { class: 'banner error mt12' }, 'Enter your registered email.')]);
      return;
    }
    state.busy = true;
    /** @type {HTMLButtonElement} */ (submit).disabled = true;
    try {
      await requestOtp(tenantId, email);
      state.tenantId = tenantId;
      state.email = email;
      state.stage = 'otp';
      state.gateNotice = '';
      renderGate();
    } catch (err) {
      replace(errHost, [errorBanner(err)]);
    } finally {
      state.busy = false;
      /** @type {HTMLButtonElement} */ (submit).disabled = false;
    }
  }

  const onEnter = (/** @type {KeyboardEvent} */ ev) => {
    if (ev.key === 'Enter') onSendOtp();
  };
  tenantInput.addEventListener('keydown', onEnter);
  emailInput.addEventListener('keydown', onEnter);

  return el('div', { class: 'gatecard' }, [
    gateHead(),
    el('div', { class: 'gate-title' }, 'Staff sign in'),
    el('div', { class: 'gate-sub' }, 'A one-time password will be sent to your registered contact. It is never shown on screen.'),
    state.gateNotice ? el('div', { class: 'banner info mb12' }, state.gateNotice) : null,
    el('div', { class: 'field' }, [el('label', { for: 'lg_tenant' }, 'Tenant ID'), tenantInput]),
    el('div', { class: 'field' }, [el('label', { for: 'lg_email' }, 'Email'), emailInput]),
    submit,
    errHost,
    el('div', { class: 'gate-note' }, 'Protected by OTP · Kenya DPA 2019 compliant'),
  ]);
}

/** @param {HTMLElement} errHost */
function gateOtpCard(errHost) {
  /** @type {HTMLInputElement[]} */
  const digits = [];
  const box = el('div', { class: 'otpbox' });
  for (let i = 0; i < 6; i += 1) {
    const input = /** @type {HTMLInputElement} */ (
      el('input', { inputmode: 'numeric', maxlength: 1, autocomplete: 'one-time-code', 'aria-label': `OTP digit ${i + 1}` })
    );
    input.addEventListener('input', () => {
      if (input.value && digits[i + 1]) digits[i + 1].focus();
    });
    input.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') onVerify();
      if (ev.key === 'Backspace' && !input.value && digits[i - 1]) digits[i - 1].focus();
    });
    digits.push(input);
    box.appendChild(input);
  }
  const submit = el('button', { class: 'btn p w100', on: { click: onVerify } }, 'Verify & sign in');

  async function onVerify() {
    if (state.busy) return;
    const code = digits.map((d) => d.value.trim()).join('');
    replace(errHost, []);
    if (!/^\d{6}$/.test(code)) {
      replace(errHost, [el('div', { class: 'banner error mt12' }, 'Enter all 6 digits.')]);
      return;
    }
    state.busy = true;
    /** @type {HTMLButtonElement} */ (submit).disabled = true;
    try {
      const tokens = /** @type {{access_token:string, refresh_token:string}} */ (
        await verifyOtp(state.tenantId, state.email, code)
      );
      setTokens(tokens.access_token, tokens.refresh_token);
      setActiveTenantId(state.tenantId);
      storeTenantId(state.tenantId);
      const perms = /** @type {{permissions: Array<{module:string,can_view:boolean,can_create:boolean,can_edit:boolean,can_approve:boolean}>}} */ (
        await myPermissions()
      );
      setPermissions(perms.permissions);
      showApp();
    } catch (err) {
      replace(errHost, [errorBanner(err)]);
    } finally {
      state.busy = false;
      /** @type {HTMLButtonElement} */ (submit).disabled = false;
    }
  }

  async function onResend() {
    replace(errHost, []);
    try {
      await requestOtp(state.tenantId, state.email);
      replace(errHost, [el('div', { class: 'banner ok mt12' }, 'A new code was sent.')]);
    } catch (err) {
      replace(errHost, [errorBanner(err)]);
    }
  }

  return el('div', { class: 'gatecard' }, [
    gateHead(),
    el('div', { class: 'gate-title' }, 'Verify OTP'),
    el('div', { class: 'gate-sub' }, `Enter the 6-digit code sent to the contact registered for ${state.email}.`),
    box,
    submit,
    errHost,
    el('div', { class: 'gate-note' }, [
      el('button', { class: 'linklike', on: { click: onResend } }, 'Resend code'),
      ' · ',
      el('button', {
        class: 'linklike',
        on: {
          click: () => {
            state.stage = 'signin';
            renderGate();
          },
        },
      }, 'Change details'),
    ]),
  ]);
}

/* ----------------------------- shell ------------------------------ */

function onSessionExpired() {
  clearSession();
  state.stage = 'signin';
  state.gateNotice = 'Session expired — sign in again.';
  renderGate();
}

async function onSignOut() {
  await logout();
  state.stage = 'signin';
  state.gateNotice = 'Signed out.';
  renderGate();
}

/** @param {string} id */
function go(id) {
  const screen = SCREENS.find((s) => s.id === id);
  if (!screen) return;
  state.activeScreen = id;
  setText(byId('h-title'), screen.title);
  setText(byId('h-sub'), screen.sub);
  renderNav();
  // Fresh wrapper nodes per mount: a LATE async render of the previous
  // screen writes into detached nodes instead of clobbering the active
  // screen or its modal host (adversarial-review finding #2).
  const host = el('div', {});
  replace(byId('main'), [host]);
  const modal = el('div', {});
  replace(byId('modal-host'), [modal]);
  screen.mount(host, { onSessionExpired, modalHost: modal });
}

function renderNav() {
  const items = SCREENS.map((s) =>
    el('button', {
      class: `nav-item${state.activeScreen === s.id ? ' on' : ''}`,
      on: { click: () => go(s.id) },
    }, s.label),
  );
  replace(byId('nav'), [el('div', { class: 'sec' }, 'Administration'), ...items]);
}

function renderWho() {
  const initials = state.email.slice(0, 2).toUpperCase() || '··';
  replace(byId('who'), [
    el('div', { class: 'av' }, initials),
    el('div', { class: 'grow' }, [
      el('div', { class: 'nm' }, state.email),
      el('div', { class: 'rl' }, 'Signed in'),
    ]),
  ]);
}

function showApp() {
  byId('gate').hidden = true;
  byId('app').hidden = false;
  renderWho();
  replace(byId('h-actions'), [el('button', { class: 'btn', on: { click: onSignOut } }, 'Sign out')]);
  if (!can('access_control', 'view')) {
    // Least disclosure: no enumeration of what other roles could see.
    replace(byId('nav'), [el('div', { class: 'sec' }, 'Administration')]);
    replace(byId('main'), [
      el('div', { class: 'card pad' }, [
        el('div', { class: 'banner info' }, 'Your role has no access to this console.'),
      ]),
    ]);
    return;
  }
  if (SCREENS.length === 0) {
    replace(byId('main'), [
      el('div', { class: 'card pad' }, [el('div', { class: 'empty' }, 'No screens registered yet.')]),
    ]);
    return;
  }
  go(SCREENS[0].id);
}

/* ------------------------------ boot ------------------------------ */

function boot() {
  const doc = globalThis.document;
  if (!doc || !doc.getElementById('gate')) return; // not the browser shell (tests/lint import)
  // Clickjacking guard (threat 6): refuse to run framed. The host must
  // additionally send frame-ancestors 'none' (README).
  if (globalThis.window && globalThis.window.top !== globalThis.window.self) {
    const warning = doc.getElementById('framed-warning');
    if (warning) warning.hidden = false;
    byId('gate').hidden = true;
    byId('app').hidden = true;
    return;
  }
  if (!isAuthenticated()) renderGate();
}

boot();
