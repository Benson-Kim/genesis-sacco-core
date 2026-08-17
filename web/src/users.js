/**
 * Users administration screen (P13.5 — prototype Access-control "Users"
 * tab, wired to the real API).
 *
 * Security posture:
 * - Every rendered string (names, emails, branches) is attacker-
 *   influenced data and goes through dom.js text-node construction.
 * - UI affordances follow the P4 matrix via session.can() — pure UX;
 *   the server enforces every call (gate 1.6). 403s render a generic
 *   "not permitted" with no capability hints.
 * - Every mutation sends the `version` the record was loaded with; a
 *   409 surfaces an explicit "record changed — reload" flow. No silent
 *   overwrite, no auto-retry with a fresher version.
 * - Idempotency keys are stable for retries of an identical submission
 *   and rotate when the submitted body changes (the middleware replays
 *   only on an identical request hash).
 * - OTP actions render side-effect COUNTS only. No secret, code or
 *   challenge content exists anywhere in this UI.
 * - Keyset pagination only (opaque cursors; no page numbers).
 */

import { el, replace } from './dom.js';
import {
  ApiError,
  changeUserStatus,
  createUser,
  errorMessage,
  getUser,
  listRoles,
  listUsers,
  newIdempotencyKey,
  assignUserRole,
  invalidateUserOtp,
  reenrolUserOtp,
  updateUser,
} from './api.js';
import { can, getOwnUserId } from './session.js';
import { CursorPager, relTime, fmtDateTime } from './format.js';

const PAGE_SIZE = 20;

/**
 * Stable idempotency key per identical logical submission: reuse the
 * key while the serialized body is unchanged (safe retry -> replay),
 * rotate when the body changes (new logical submission).
 * @param {{key:string|null, body:string|null}} slot
 * @param {string} bodyJson
 */
export function idempotencyKeyFor(slot, bodyJson, fresh = newIdempotencyKey) {
  if (slot.key === null || slot.body !== bodyJson) {
    slot.key = fresh();
    slot.body = bodyJson;
  }
  return slot.key;
}

/**
 * @param {unknown} err
 * @returns {HTMLElement}
 */
function errBanner(err) {
  const { title, correlationId } = errorMessage(err);
  const fields = err instanceof ApiError && err.fields.length > 0
    ? el('ul', { class: 'form-errors' }, err.fields.map((f) => el('li', {}, `${f.field}: ${f.message}`)))
    : null;
  return el('div', { class: 'banner error mt12' }, [
    title,
    fields,
    correlationId ? el('span', { class: 'cid' }, `ref: ${correlationId}`) : null,
  ]);
}

/** @param {string} status */
function statusPill(status) {
  const cls = status === 'active' ? 'ok' : 'bad';
  const label = status === 'active' ? 'Active' : status === 'suspended' ? 'Suspended' : status;
  return el('span', { class: `pill ${cls}` }, label);
}

/** @param {string} name */
function initialsAvatar(name) {
  const initials = name
    .split(' ')
    .filter(Boolean)
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || '·';
  return el('div', { class: 'av' }, initials);
}

/**
 * @param {HTMLElement} container
 * @param {{onSessionExpired:()=>void, modalHost:HTMLElement}} ctx
 */
export function mountUsers(container, ctx) {
  const state = {
    pager: new CursorPager(),
    /** @type {{status:string, role_id:string}} */
    filters: { status: '', role_id: '' },
    /** @type {Array<Record<string, unknown>>} */
    items: [],
    /** @type {Array<{id:string, name:string}>} */
    roles: [],
    loading: false,
    /** @type {unknown} */ listError: null,
    /** current drawer: null | create | detail-of-user */
    /** @type {null | {mode:'create'} | {mode:'detail', user:Record<string,unknown>, editing:boolean, conflict:boolean, notice:string, error:unknown, busy:boolean}} */
    drawer: null,
    /** @type {null | {kind:'status'|'role'|'otp-invalidate'|'otp-reenrol', user:Record<string,unknown>, targetStatus?:string, roleId?:string, error:unknown, busy:boolean, result?:Record<string,unknown>}} */
    confirm: null,
    idemSlot: { key: /** @type {string|null} */ (null), body: /** @type {string|null} */ (null) },
  };

  /** @param {unknown} err @param {() => void} onOther */
  function handleAuthOr(err, onOther) {
    if (err instanceof ApiError && err.status === 401) {
      ctx.onSessionExpired();
      return;
    }
    onOther();
  }

  async function load(/** @type {string|null} */ cursor) {
    state.loading = true;
    state.listError = null;
    render();
    try {
      const page = /** @type {{items:Array<Record<string,unknown>>, next_cursor:string|null}} */ (
        await listUsers({
          cursor,
          limit: PAGE_SIZE,
          status: state.filters.status || undefined,
          role_id: state.filters.role_id || undefined,
        })
      );
      state.items = page.items;
      state.pager.setNext(page.next_cursor);
    } catch (err) {
      handleAuthOr(err, () => {
        state.listError = err;
      });
    } finally {
      state.loading = false;
      render();
    }
  }

  async function loadRoles() {
    try {
      const roles = /** @type {Array<{id:string,name:string}>} */ (await listRoles());
      state.roles = roles.map((r) => ({ id: r.id, name: r.name }));
    } catch (err) {
      // Role names degrade gracefully; the list still works.
      handleAuthOr(err, () => {});
    }
    render();
  }

  function resetAndLoad() {
    state.pager.reset();
    load(null);
  }

  /* --------------------------- rendering -------------------------- */

  function render() {
    replace(container, [
      el('div', { class: 'card clip' }, [toolbar(), table(), pagerBar()]),
    ]);
    replace(ctx.modalHost, [
      state.confirm ? confirmDialog() : state.drawer ? drawer() : null,
    ]);
  }

  function toolbar() {
    const statusSel = el('select', { 'aria-label': 'Filter by status' }, [
      el('option', { value: '' }, 'All statuses'),
      el('option', { value: 'active', selected: state.filters.status === 'active' }, 'Active'),
      el('option', { value: 'suspended', selected: state.filters.status === 'suspended' }, 'Suspended'),
    ]);
    statusSel.addEventListener('change', () => {
      state.filters.status = /** @type {HTMLSelectElement} */ (statusSel).value;
      resetAndLoad();
    });
    const roleSel = el('select', { 'aria-label': 'Filter by role' }, [
      el('option', { value: '' }, 'All roles'),
      ...state.roles.map((r) =>
        el('option', { value: r.id, selected: state.filters.role_id === r.id }, r.name),
      ),
    ]);
    roleSel.addEventListener('change', () => {
      state.filters.role_id = /** @type {HTMLSelectElement} */ (roleSel).value;
      resetAndLoad();
    });
    return el('div', { class: 'toolbar' }, [
      el('div', { class: 'filters' }, [
        el('div', { class: 'field' }, [el('label', {}, 'Status'), statusSel]),
        el('div', { class: 'field' }, [el('label', {}, 'Role'), roleSel]),
      ]),
      can('access_control', 'create')
        ? el('button', {
            class: 'btn p',
            on: {
              click: () => {
                state.confirm = null;
                state.drawer = { mode: 'create' };
                render();
              },
            },
          }, '+ Add user')
        : null,
    ]);
  }

  function table() {
    if (state.loading) return el('div', { class: 'empty' }, 'Loading…');
    if (state.listError) return el('div', { class: 'pad' }, [errBanner(state.listError)]);
    if (state.items.length === 0) return el('div', { class: 'empty' }, 'No users match the current filters.');
    const head = el('thead', {}, [
      el('tr', {}, [
        el('th', {}, 'User'),
        el('th', {}, 'Role'),
        el('th', {}, 'Branch'),
        el('th', { class: 'r' }, 'Last active'),
        el('th', { class: 'r' }, 'Status'),
        el('th', {}, ''),
      ]),
    ]);
    const rows = state.items.map((u) => {
      const row = el('tr', { class: 'rowbtn' }, [
        el('td', {}, [
          el('div', { class: 'mrow' }, [
            initialsAvatar(String(u.full_name ?? '')),
            el('div', {}, [
              el('div', { class: 'cell-strong' }, String(u.full_name ?? '')),
              el('div', { class: 'cell-sub' }, String(u.email ?? '')),
            ]),
          ]),
        ]),
        el('td', {}, [el('span', { class: 'pill neutral' }, String(u.role_name ?? ''))]),
        el('td', { class: 'muted' }, u.branch ? String(u.branch) : '—'),
        el('td', { class: 'r muted' }, relTime(/** @type {string|null} */ (u.last_active_at))),
        el('td', { class: 'r' }, [statusPill(String(u.status ?? ''))]),
        el('td', { class: 'r muted' }, '›'),
      ]);
      row.addEventListener('click', () => openDetail(String(u.id)));
      return row;
    });
    return el('table', {}, [head, el('tbody', {}, rows)]);
  }

  function pagerBar() {
    return el('div', { class: 'pagerbar' }, [
      el('div', { class: 'note' }, 'Keyset pagination — newest first'),
      el('button', {
        class: 'btn sm',
        disabled: !state.pager.hasPrev() || state.loading,
        on: {
          click: () => {
            load(state.pager.goPrev());
          },
        },
      }, '‹ Prev'),
      el('button', {
        class: 'btn sm',
        disabled: !state.pager.hasNext() || state.loading,
        on: {
          click: () => {
            load(state.pager.goNext());
          },
        },
      }, 'Next ›'),
    ]);
  }

  /* ---------------------------- detail ----------------------------- */

  async function openDetail(/** @type {string} */ userId) {
    state.drawer = null;
    render();
    try {
      const user = /** @type {Record<string, unknown>} */ (await getUser(userId));
      state.drawer = { mode: 'detail', user, editing: false, conflict: false, notice: '', error: null, busy: false };
    } catch (err) {
      handleAuthOr(err, () => {
        state.listError = err;
      });
    }
    render();
  }

  async function reloadDetail() {
    const d = state.drawer;
    if (!d || d.mode !== 'detail') return;
    try {
      d.user = /** @type {Record<string, unknown>} */ (await getUser(String(d.user.id)));
      d.conflict = false;
      d.error = null;
      d.notice = 'Record reloaded — re-apply your change against the current values.';
    } catch (err) {
      handleAuthOr(err, () => {
        d.error = err;
      });
    }
    render();
  }

  function closeDrawer() {
    state.drawer = null;
    render();
    resetAndLoad();
  }

  function drawer() {
    const d = state.drawer;
    if (!d) return null;
    const overlay = el('div', { class: 'modal' });
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay) closeDrawer();
    });
    const body = d.mode === 'create' ? createForm() : d.editing ? editForm(d) : detailView(d);
    const panel = el('div', { class: 'drawer' }, [
      el('div', { class: 'drawer-head' }, [
        el('div', { class: 't' }, d.mode === 'create' ? 'Add user' : d.editing ? 'Edit profile' : 'User detail'),
        el('button', { class: 'btn sm', on: { click: closeDrawer } }, '✕'),
      ]),
      body,
    ]);
    overlay.appendChild(panel);
    return overlay;
  }

  /** @param {{user:Record<string,unknown>, conflict:boolean, notice:string, error:unknown, busy:boolean}} d */
  function detailView(d) {
    const u = d.user;
    const own = getOwnUserId() !== null && getOwnUserId() === String(u.id);
    const mayEdit = can('access_control', 'edit');
    const suspendTarget = String(u.status) === 'active' ? 'suspended' : 'active';
    /** @param {'status'|'role'|'otp-invalidate'|'otp-reenrol'} kind */
    const openConfirm = (kind) => {
      state.confirm = {
        kind,
        user: u,
        targetStatus: kind === 'status' ? suspendTarget : undefined,
        roleId: kind === 'role' ? String(u.role_id) : undefined,
        error: null,
        busy: false,
      };
      state.idemSlot = { key: null, body: null };
      render();
    };
    return el('div', {}, [
      d.notice ? el('div', { class: 'banner info mb12' }, d.notice) : null,
      d.error ? errBanner(d.error) : null,
      el('div', { class: 'mrow mb12' }, [
        initialsAvatar(String(u.full_name ?? '')),
        el('div', {}, [
          el('div', { class: 'cell-strong' }, String(u.full_name ?? '')),
          el('div', { class: 'cell-sub' }, String(u.email ?? '')),
        ]),
      ]),
      el('div', { class: 'detail-grid' }, [
        kv('Role', String(u.role_name ?? '')),
        kv('Branch', u.branch ? String(u.branch) : '—'),
        kv('Phone', u.phone ? String(u.phone) : '—'),
        kv('Status', String(u.status ?? '')),
        kv('Last active', fmtDateTime(/** @type {string|null} */ (u.last_active_at))),
        kv('Record version', String(u.version ?? '')),
      ]),
      own ? el('div', { class: 'banner info mt12' }, 'This is your own account. Role and status changes require another administrator (separation of duties).') : null,
      mayEdit
        ? el('div', { class: 'mt16' }, [
            el('div', { class: 'subhead' }, 'Actions'),
            el('div', { class: 'actions' }, [
              el('button', {
                class: 'btn',
                on: {
                  click: () => {
                    d.editing = true;
                    d.notice = '';
                    d.error = null;
                    state.idemSlot = { key: null, body: null };
                    render();
                  },
                },
              }, 'Edit profile'),
              el('button', { class: 'btn', disabled: own, on: { click: () => openConfirm('role') } }, 'Change role'),
              el('button', {
                class: `btn ${suspendTarget === 'suspended' ? 'danger' : 'g'}`,
                disabled: own,
                on: { click: () => openConfirm('status') },
              }, suspendTarget === 'suspended' ? 'Suspend' : 'Activate'),
            ]),
            el('div', { class: 'subhead mt16' }, 'OTP / credentials'),
            el('div', { class: 'actions' }, [
              el('button', { class: 'btn', on: { click: () => openConfirm('otp-invalidate') } }, 'Invalidate pending OTP'),
              el('button', { class: 'btn', on: { click: () => openConfirm('otp-reenrol') } }, 'Force OTP re-enrolment'),
            ]),
            el('div', { class: 'cell-sub mt8' }, 'OTP codes are never shown — these actions void pending challenges and (for re-enrolment) revoke sessions; the user signs in again via a fresh OTP.'),
          ])
        : null,
    ]);
  }

  /** @param {string} k @param {string} v */
  function kv(k, v) {
    return el('div', { class: 'row' }, [el('span', { class: 'k' }, k), el('span', { class: 'val' }, v)]);
  }

  /** @param {{user:Record<string,unknown>, conflict:boolean, notice:string, error:unknown, busy:boolean}} d */
  function editForm(d) {
    const u = d.user;
    const nameIn = el('input', { value: String(u.full_name ?? ''), maxlength: 200 });
    const emailIn = el('input', { type: 'email', value: String(u.email ?? ''), maxlength: 254 });
    const phoneIn = el('input', { value: u.phone ? String(u.phone) : '', maxlength: 32 });
    const branchIn = el('input', { value: u.branch ? String(u.branch) : '', maxlength: 120 });
    const save = el('button', { class: 'btn p w100 mt12', on: { click: onSave } }, 'Save changes');

    async function onSave() {
      if (d.busy) return;
      const body = {
        version: Number(u.version),
        full_name: /** @type {HTMLInputElement} */ (nameIn).value.trim(),
        email: /** @type {HTMLInputElement} */ (emailIn).value.trim(),
        phone: /** @type {HTMLInputElement} */ (phoneIn).value.trim() || null,
        branch: /** @type {HTMLInputElement} */ (branchIn).value.trim() || null,
      };
      const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'update', id: u.id, body }));
      d.busy = true;
      d.error = null;
      d.conflict = false;
      render();
      try {
        const updated = /** @type {Record<string, unknown>} */ (
          await updateUser(String(u.id), body, key)
        );
        state.drawer = { mode: 'detail', user: updated, editing: false, conflict: false, notice: 'Profile saved.', error: null, busy: false };
      } catch (err) {
        handleAuthOr(err, () => {
          if (err instanceof ApiError && err.status === 409) {
            d.conflict = true;
          } else {
            d.error = err;
          }
        });
      } finally {
        d.busy = false;
        render();
      }
    }

    return el('div', {}, [
      d.conflict
        ? el('div', { class: 'banner error mb12' }, [
            'This record changed since you loaded it (or conflicts with an existing one). Your change was NOT applied.',
            el('div', { class: 'mt8' }, [
              el('button', { class: 'btn sm', on: { click: reloadDetail } }, 'Reload record'),
            ]),
          ])
        : null,
      d.error ? errBanner(d.error) : null,
      el('div', { class: 'field' }, [el('label', {}, 'Full name'), nameIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Email'), emailIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Phone'), phoneIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Branch'), branchIn]),
      el('div', { class: 'cell-sub' }, `Optimistic lock: saving against record version ${String(u.version)}.`),
      save,
      el('button', {
        class: 'btn w100 mt8',
        on: {
          click: () => {
            d.editing = false;
            d.error = null;
            d.conflict = false;
            render();
          },
        },
      }, 'Cancel'),
    ]);
  }

  function createForm() {
    const nameIn = el('input', { maxlength: 200, placeholder: 'Jane Muthoni' });
    const emailIn = el('input', { type: 'email', maxlength: 254, placeholder: 'jane@sacco.co.ke' });
    const phoneIn = el('input', { maxlength: 32, placeholder: '+2547…' });
    const branchIn = el('input', { maxlength: 120, placeholder: 'Nairobi CBD' });
    const roleSel = el('select', {}, state.roles.map((r) => el('option', { value: r.id }, r.name)));
    const errHost = el('div', {});
    const save = el('button', { class: 'btn p w100 mt12', on: { click: onCreate } }, 'Create user');
    let busy = false;

    async function onCreate() {
      if (busy) return;
      const body = {
        full_name: /** @type {HTMLInputElement} */ (nameIn).value.trim(),
        email: /** @type {HTMLInputElement} */ (emailIn).value.trim(),
        role_id: /** @type {HTMLSelectElement} */ (roleSel).value,
        phone: /** @type {HTMLInputElement} */ (phoneIn).value.trim() || null,
        branch: /** @type {HTMLInputElement} */ (branchIn).value.trim() || null,
      };
      if (!body.full_name || !body.email || !body.role_id) {
        replace(errHost, [el('div', { class: 'banner error mt12' }, 'Full name, email and role are required.')]);
        return;
      }
      const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'create', body }));
      busy = true;
      /** @type {HTMLButtonElement} */ (save).disabled = true;
      replace(errHost, []);
      try {
        const created = /** @type {Record<string, unknown>} */ (await createUser(body, key));
        state.drawer = { mode: 'detail', user: created, editing: false, conflict: false, notice: 'User created.', error: null, busy: false };
        state.idemSlot = { key: null, body: null };
        render();
      } catch (err) {
        handleAuthOr(err, () => {
          replace(errHost, [errBanner(err)]);
        });
      } finally {
        busy = false;
        /** @type {HTMLButtonElement} */ (save).disabled = false;
      }
    }

    return el('div', {}, [
      el('div', { class: 'field' }, [el('label', {}, 'Full name'), nameIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Email'), emailIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Role'), roleSel]),
      el('div', { class: 'field' }, [el('label', {}, 'Phone (optional)'), phoneIn]),
      el('div', { class: 'field' }, [el('label', {}, 'Branch (optional)'), branchIn]),
      el('div', { class: 'cell-sub' }, 'The new user signs in with an OTP sent to their registered contact. No credential is displayed or emailed by this console.'),
      save,
      errHost,
    ]);
  }

  /* --------------------------- confirms ---------------------------- */

  function confirmDialog() {
    const c = state.confirm;
    if (!c) return null;
    const u = c.user;
    const overlay = el('div', { class: 'modal center' });
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay && !c.busy) {
        state.confirm = null;
        render();
      }
    });

    /** @type {HTMLElement[]} */
    let content = [];
    let confirmLabel = 'Confirm';
    let confirmClass = 'btn p';
    /** @type {() => Promise<unknown>} */
    let action = async () => null;

    if (c.kind === 'status') {
      const suspending = c.targetStatus === 'suspended';
      confirmLabel = suspending ? 'Suspend user' : 'Activate user';
      confirmClass = suspending ? 'btn danger' : 'btn g';
      content = [
        el('div', { class: 'banner info mb12' }, suspending
          ? 'Suspending takes effect immediately: pending OTP challenges are voided and every session is revoked in the same transaction.'
          : 'Activating restores sign-in for this user.'),
        kv('User', String(u.full_name ?? '')),
        kv('Current status', String(u.status ?? '')),
        kv('New status', String(c.targetStatus)),
      ];
      action = () => {
        const body = { version: Number(u.version), status: String(c.targetStatus) };
        const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'status', id: u.id, body }));
        return changeUserStatus(String(u.id), body, key);
      };
    } else if (c.kind === 'role') {
      const roleSel = el('select', {}, state.roles.map((r) =>
        el('option', { value: r.id, selected: r.id === c.roleId }, r.name),
      ));
      roleSel.addEventListener('change', () => {
        c.roleId = /** @type {HTMLSelectElement} */ (roleSel).value;
      });
      confirmLabel = 'Assign role';
      content = [
        el('div', { class: 'banner info mb12' }, 'Role assignment is audited with before/after. The last active System Admin cannot be re-roled away.'),
        kv('User', String(u.full_name ?? '')),
        kv('Current role', String(u.role_name ?? '')),
        el('div', { class: 'field mt12' }, [el('label', {}, 'New role'), roleSel]),
      ];
      action = () => {
        const body = { version: Number(u.version), role_id: String(c.roleId) };
        const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'role', id: u.id, body }));
        return assignUserRole(String(u.id), body, key);
      };
    } else if (c.kind === 'otp-invalidate') {
      confirmLabel = 'Invalidate OTP challenges';
      content = [
        el('div', { class: 'banner info mb12' }, 'Voids every pending OTP challenge for this user. No code is ever displayed — the user simply requests a fresh OTP at sign-in.'),
        kv('User', String(u.full_name ?? '')),
      ];
      action = () => {
        const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'otp-invalidate', id: u.id }));
        return invalidateUserOtp(String(u.id), key);
      };
    } else {
      confirmLabel = 'Force re-enrolment';
      confirmClass = 'btn danger';
      content = [
        el('div', { class: 'banner info mb12' }, 'Voids pending OTP challenges AND revokes every session, forcing a fresh OTP sign-in. Nothing secret is shown or sent from this console.'),
        kv('User', String(u.full_name ?? '')),
      ];
      action = () => {
        const key = idempotencyKeyFor(state.idemSlot, JSON.stringify({ op: 'otp-reenrol', id: u.id }));
        return reenrolUserOtp(String(u.id), key);
      };
    }

    async function onConfirm() {
      if (c.busy) return;
      c.busy = true;
      c.error = null;
      render();
      try {
        const result = /** @type {Record<string, unknown>} */ (await action());
        if (c.kind === 'otp-invalidate' || c.kind === 'otp-reenrol') {
          c.result = result;
          c.busy = false;
          render();
          return;
        }
        // status/role return the updated user: refresh the open drawer.
        state.confirm = null;
        state.drawer = { mode: 'detail', user: result, editing: false, conflict: false, notice: 'Change applied.', error: null, busy: false };
        render();
        resetAndLoad();
      } catch (err) {
        handleAuthOr(err, () => {
          c.error = err;
        });
        c.busy = false;
        render();
      }
    }

    const resultView = c.result
      ? el('div', {}, [
          el('div', { class: 'banner ok mb12' }, 'Done.'),
          ...Object.entries(c.result).map(([k, v]) => kv(k.replace(/_/g, ' '), String(v))),
          el('button', {
            class: 'btn w100 mt12',
            on: {
              click: () => {
                state.confirm = null;
                render();
              },
            },
          }, 'Close'),
        ])
      : null;

    const conflict409 = c.error instanceof ApiError && c.error.status === 409;
    const dialog = el('div', { class: 'dialog' }, [
      el('div', { class: 'drawer-head' }, [
        el('div', { class: 't' }, confirmLabel),
        el('button', {
          class: 'btn sm',
          disabled: c.busy,
          on: {
            click: () => {
              state.confirm = null;
              render();
            },
          },
        }, '✕'),
      ]),
      ...(resultView ? [resultView] : [
        ...content,
        c.error
          ? el('div', {}, [
              errBanner(c.error),
              conflict409
                ? el('div', { class: 'mt8' }, [
                    el('button', {
                      class: 'btn sm',
                      on: {
                        click: async () => {
                          // Explicit reload flow: fetch current record,
                          // then the operator re-initiates the action.
                          state.confirm = null;
                          render();
                          await openDetail(String(u.id));
                        },
                      },
                    }, 'Reload record'),
                  ])
                : null,
            ])
          : null,
        el('div', { class: 'actions mt16' }, [
          el('button', {
            class: 'btn',
            disabled: c.busy,
            on: {
              click: () => {
                state.confirm = null;
                render();
              },
            },
          }, 'Cancel'),
          el('button', { class: confirmClass, disabled: c.busy, on: { click: onConfirm } }, confirmLabel),
        ]),
      ]),
    ]);
    overlay.appendChild(dialog);
    return overlay;
  }

  /* ----------------------------- boot ------------------------------ */

  render();
  loadRoles();
  load(null);
}
