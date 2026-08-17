/**
 * Pure formatting helpers + keyset pager (no DOM, no I/O — testable).
 *
 * Audit payload handling (named threat): payloads are rendered as
 * pretty-printed JSON TEXT exactly as the API returned them. There is
 * no client-side reconstruction of redacted fields and no
 * interpretation of payload contents.
 */

/**
 * Pretty-print an API-provided JSON value as plain text.
 * @param {unknown} value
 * @returns {string}
 */
export function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * Human-readable timestamp from an ISO string; falls back to the raw
 * string (rendered as text) if unparseable.
 * @param {string|null|undefined} iso
 */
export function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return new Intl.DateTimeFormat('en-KE', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(d);
}

/**
 * Compact relative time ("2m ago") for last-active columns.
 * @param {string|null|undefined} iso
 * @param {Date} [now]
 */
export function relTime(iso, now = new Date()) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const sec = Math.max(0, Math.floor((now.getTime() - d.getTime()) / 1000));
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

/**
 * Keyset pagination state (gate 1.3 — cursors only, never offsets or
 * page numbers). Cursors are OPAQUE server strings: stored and echoed
 * back, never parsed or rendered.
 */
export class CursorPager {
  constructor() {
    /** @type {string[]} cursors of previous pages (for ‹ Prev) */
    this.stack = [];
    /** @type {string|null} cursor that produced the current page */
    this.current = null;
    /** @type {string|null} cursor for the next page, from the API */
    this.next = null;
  }

  reset() {
    this.stack = [];
    this.current = null;
    this.next = null;
  }

  /** @param {string|null} nextCursor */
  setNext(nextCursor) {
    this.next = nextCursor;
  }

  hasNext() {
    return this.next !== null;
  }

  hasPrev() {
    return this.current !== null;
  }

  /** Advance; returns the cursor to request. */
  goNext() {
    if (this.next === null) throw new Error('pager: no next page');
    if (this.current !== null) this.stack.push(this.current);
    else this.stack.push('');
    this.current = this.next;
    this.next = null;
    return this.current;
  }

  /** Go back; returns the cursor to request (null = first page). */
  goPrev() {
    const prev = this.stack.pop();
    if (prev === undefined) {
      this.current = null;
      return null;
    }
    this.current = prev === '' ? null : prev;
    this.next = null;
    return this.current;
  }
}
