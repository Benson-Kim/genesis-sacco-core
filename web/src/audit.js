/**
 * Audit-log viewer (P13.5 — the governance counterpart of the
 * audit-write gate 1.5).
 *
 * Security posture:
 * - before/after payloads are ATTACKER-INFLUENCED data (they embed
 *   member names, branches, purposes…). They render as pretty-printed
 *   JSON assigned via textContent into a <pre> — exactly as the API
 *   returned them. No client-side reconstruction of redacted fields,
 *   no interpretation, no HTML parsing (the named XSS threat).
 * - Keyset pagination only: the cursor is an opaque server string,
 *   echoed back as a query parameter, never parsed or rendered.
 * - Filters are length/shape-limited client-side (UUID check for the
 *   actor filter avoids 422 noise); the server validates and enforces
 *   entitlement regardless.
 */

import { el, replace } from './dom.js';
import { ApiError, errorMessage, isUuid, listAuditLog } from './api.js';
import { CursorPager, fmtDateTime, prettyJson } from './format.js';

const PAGE_SIZE = 20;

/**
 * @param {unknown} err
 * @returns {HTMLElement}
 */
function errBanner(err) {
  const { title, correlationId } = errorMessage(err);
  return el('div', { class: 'banner error mt12' }, [
    title,
    correlationId ? el('span', { class: 'cid' }, `ref: ${correlationId}`) : null,
  ]);
}

/**
 * @param {HTMLElement} container
 * @param {{onSessionExpired:()=>void, modalHost:HTMLElement}} ctx
 */
export function mountAudit(container, ctx) {
  const state = {
    pager: new CursorPager(),
    filters: { entity: '', actor_id: '', action: '', date_from: '', date_to: '' },
    /** @type {Array<Record<string, unknown>>} */
    items: [],
    loading: false,
    /** @type {unknown} */ listError: null,
    /** @type {string} client-side filter validation message */
    filterError: '',
    /** @type {Record<string, unknown>|null} entry shown in the drawer */
    selected: null,
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
        await listAuditLog({
          cursor,
          limit: PAGE_SIZE,
          entity: state.filters.entity || undefined,
          actor_id: state.filters.actor_id || undefined,
          action: state.filters.action || undefined,
          date_from: state.filters.date_from || undefined,
          date_to: state.filters.date_to || undefined,
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

  function resetAndLoad() {
    state.pager.reset();
    load(null);
  }

  /* --------------------------- rendering -------------------------- */

  function render() {
    replace(container, [
      el('div', { class: 'card clip' }, [toolbar(), table(), pagerBar()]),
    ]);
    replace(ctx.modalHost, [state.selected ? detailDrawer(state.selected) : null]);
  }

  function toolbar() {
    const entityIn = el('input', { value: state.filters.entity, maxlength: 100, placeholder: 'e.g. users' });
    const actionIn = el('input', { value: state.filters.action, maxlength: 100, placeholder: 'e.g. user.role' });
    const actorIn = el('input', { value: state.filters.actor_id, maxlength: 36, placeholder: 'actor UUID' });
    const fromIn = el('input', { type: 'date', value: state.filters.date_from });
    const toIn = el('input', { type: 'date', value: state.filters.date_to });

    function apply() {
      const actor = /** @type {HTMLInputElement} */ (actorIn).value.trim();
      if (actor && !isUuid(actor)) {
        state.filterError = 'Actor filter must be a UUID.';
        render();
        return;
      }
      state.filterError = '';
      state.filters = {
        entity: /** @type {HTMLInputElement} */ (entityIn).value.trim(),
        action: /** @type {HTMLInputElement} */ (actionIn).value.trim(),
        actor_id: actor,
        date_from: /** @type {HTMLInputElement} */ (fromIn).value,
        date_to: /** @type {HTMLInputElement} */ (toIn).value,
      };
      resetAndLoad();
    }

    function clearFilters() {
      state.filterError = '';
      state.filters = { entity: '', actor_id: '', action: '', date_from: '', date_to: '' };
      resetAndLoad();
    }

    return el('div', {}, [
      el('div', { class: 'toolbar' }, [
        el('div', { class: 'filters' }, [
          el('div', { class: 'field' }, [el('label', {}, 'Entity'), entityIn]),
          el('div', { class: 'field' }, [el('label', {}, 'Action'), actionIn]),
          el('div', { class: 'field' }, [el('label', {}, 'Actor'), actorIn]),
          el('div', { class: 'field' }, [el('label', {}, 'From'), fromIn]),
          el('div', { class: 'field' }, [el('label', {}, 'To'), toIn]),
        ]),
        el('div', { class: 'actions' }, [
          el('button', { class: 'btn', on: { click: clearFilters } }, 'Clear'),
          el('button', { class: 'btn p', on: { click: apply } }, 'Apply filters'),
        ]),
      ]),
      state.filterError ? el('div', { class: 'pad' }, [el('div', { class: 'banner error' }, state.filterError)]) : null,
    ]);
  }

  function table() {
    if (state.loading) return el('div', { class: 'empty' }, 'Loading…');
    if (state.listError) return el('div', { class: 'pad' }, [errBanner(state.listError)]);
    if (state.items.length === 0) return el('div', { class: 'empty' }, 'No audit entries match the current filters.');
    const head = el('thead', {}, [
      el('tr', {}, [
        el('th', {}, 'At'),
        el('th', {}, 'Actor'),
        el('th', {}, 'Action'),
        el('th', {}, 'Entity'),
        el('th', {}, 'Entity id'),
        el('th', { class: 'r' }, 'Payload'),
      ]),
    ]);
    const rows = state.items.map((entry) => {
      const redacted = entry.redacted === true;
      const row = el('tr', { class: 'rowbtn' }, [
        el('td', { class: 'muted tnum' }, fmtDateTime(/** @type {string} */ (entry.at))),
        el('td', { class: 'cell-sub' }, entry.actor_id ? String(entry.actor_id) : 'system'),
        el('td', {}, [el('span', { class: 'pill neutral' }, String(entry.action ?? ''))]),
        el('td', { class: 'cell-strong' }, String(entry.entity ?? '')),
        el('td', { class: 'cell-sub' }, String(entry.entity_id ?? '')),
        el('td', { class: 'r' }, [
          redacted
            ? el('span', { class: 'pill muted' }, 'Redacted')
            : el('span', { class: 'pill ok' }, 'View ›'),
        ]),
      ]);
      row.addEventListener('click', () => {
        state.selected = entry;
        render();
      });
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

  /** @param {Record<string, unknown>} entry */
  function detailDrawer(entry) {
    const overlay = el('div', { class: 'modal' });
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay) {
        state.selected = null;
        render();
      }
    });
    const redacted = entry.redacted === true;

    /** @param {string} label @param {unknown} payload */
    function payloadBlock(label, payload) {
      return el('div', {}, [
        el('div', { class: 'payload-label' }, label),
        payload === null || payload === undefined
          ? el('div', { class: 'cell-sub' }, redacted ? 'Redacted per role entitlement.' : '—')
          : el('pre', { class: 'payload' }, prettyJson(payload)),
      ]);
    }

    const panel = el('div', { class: 'drawer' }, [
      el('div', { class: 'drawer-head' }, [
        el('div', { class: 't' }, 'Audit entry'),
        el('button', {
          class: 'btn sm',
          on: {
            click: () => {
              state.selected = null;
              render();
            },
          },
        }, '✕'),
      ]),
      el('div', { class: 'detail-grid' }, [
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'At'), el('span', { class: 'val tnum' }, fmtDateTime(/** @type {string} */ (entry.at)))]),
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'Actor'), el('span', { class: 'val' }, entry.actor_id ? String(entry.actor_id) : 'system')]),
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'Action'), el('span', { class: 'val' }, String(entry.action ?? ''))]),
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'Entity'), el('span', { class: 'val' }, String(entry.entity ?? ''))]),
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'Entity id'), el('span', { class: 'val' }, String(entry.entity_id ?? ''))]),
        el('div', { class: 'row' }, [el('span', { class: 'k' }, 'Payload access'), el('span', { class: 'val' }, redacted ? 'Redacted' : 'Disclosed per role')]),
      ]),
      redacted
        ? el('div', { class: 'banner info mt12' }, 'Payload redacted: your role cannot view the module owning this entity. Row metadata is the audit trail itself.')
        : null,
      payloadBlock('Before', entry.before),
      payloadBlock('After', entry.after),
    ]);
    overlay.appendChild(panel);
    return overlay;
  }

  /* ----------------------------- boot ------------------------------ */

  render();
  load(null);
}
