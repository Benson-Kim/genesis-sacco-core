/**
 * Safe DOM construction — the ONLY module that builds DOM nodes.
 *
 * XSS via audit-payload rendering is a named P13.5 threat: every string
 * from the API (names, branches, audit before/after JSON) is
 * attacker-influenced. This module never hands markup to the HTML
 * parser: elements come from createElement, text from createTextNode /
 * textContent, so hostile input stays inert character data by
 * construction. scripts/lint.mjs makes the parser sinks build failures.
 *
 * Attributes are allowlisted; event handlers attach via addEventListener
 * ({ on: { click: fn } }), so no string ever becomes code. Anchors and
 * dynamic URLs are deliberately unsupported (script-scheme URL vector).
 */

const ALLOWED_ATTRS = new Set([
  'class',
  'id',
  'type',
  'value',
  'placeholder',
  'title',
  'for',
  'name',
  'colspan',
  'rowspan',
  'disabled',
  'selected',
  'checked',
  'required',
  'maxlength',
  'minlength',
  'inputmode',
  'autocomplete',
  'pattern',
  'min',
  'max',
  'step',
  'rows',
  'role',
  'tabindex',
  'aria-label',
  'aria-live',
  'aria-hidden',
  'datetime',
]);

/**
 * Create an element. Children that are strings/numbers become TEXT
 * nodes — never parsed markup.
 * @param {string} tag
 * @param {Record<string, unknown>} [attrs]
 * @param {Array<unknown>|unknown} [children]
 * @returns {HTMLElement}
 */
export function el(tag, attrs = {}, children = []) {
  const doc = globalThis.document;
  const node = doc.createElement(tag);
  for (const [key, val] of Object.entries(attrs)) {
    if (key === 'on') {
      for (const [event, handler] of Object.entries(/** @type {Record<string, EventListener>} */ (val))) {
        node.addEventListener(event, handler);
      }
      continue;
    }
    if (!ALLOWED_ATTRS.has(key)) {
      throw new Error(`dom.el: attribute not allowlisted: ${key}`);
    }
    if (val === null || val === undefined || val === false) continue;
    node.setAttribute(key, val === true ? '' : String(val));
  }
  append(node, children);
  return node;
}

/**
 * Append children; strings become text nodes.
 * @param {HTMLElement} node
 * @param {Array<unknown>|unknown} children
 */
export function append(node, children) {
  const doc = globalThis.document;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === 'string' || typeof child === 'number') {
      node.appendChild(doc.createTextNode(String(child)));
    } else {
      node.appendChild(/** @type {Node} */ (child));
    }
  }
}

/**
 * Remove every child of a node.
 * @param {HTMLElement} node
 */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/**
 * Replace a node's children with the given ones.
 * @param {HTMLElement} node
 * @param {Array<unknown>|unknown} children
 */
export function replace(node, children) {
  clear(node);
  append(node, children);
}

/**
 * Set plain text content (explicit, greppable alternative to ad-hoc
 * property writes).
 * @param {HTMLElement} node
 * @param {unknown} text
 */
export function setText(node, text) {
  node.textContent = text === null || text === undefined ? '' : String(text);
}
