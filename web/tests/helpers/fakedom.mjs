/**
 * Minimal fake DOM for adversarial tests.
 *
 * Deliberate property: every HTML-parser sink (innerHTML, outerHTML,
 * insertAdjacentHTML) THROWS. If any production code path ever tries
 * to parse markup — the exact regression the P13.5 threat model bans —
 * the test run fails. That makes the XSS defence falsifiable: swap a
 * textContent assignment for innerHTML in src/ and these suites break.
 */

export class FakeTextNode {
  /** @param {string} data */
  constructor(data) {
    this.nodeType = 3;
    this.data = data;
  }
}

export class FakeElement {
  /** @param {string} tagName */
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    /** @type {Record<string, string>} */
    this.attrs = {};
    /** @type {Array<FakeElement|FakeTextNode>} */
    this.children = [];
    /** @type {Record<string, Array<(ev: unknown) => unknown>>} */
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(this.tagName)) {
      this.value = '';
    }
    const boom = (/** @type {string} */ what) => () => {
      throw new Error(`FORBIDDEN HTML-parser sink used: ${what}`);
    };
    Object.defineProperty(this, 'innerHTML', { get: boom('innerHTML'), set: boom('innerHTML') });
    Object.defineProperty(this, 'outerHTML', { get: boom('outerHTML'), set: boom('outerHTML') });
    this.insertAdjacentHTML = boom('insertAdjacentHTML');
  }

  /** @param {FakeElement|FakeTextNode} child */
  appendChild(child) {
    this.children.push(child);
    return child;
  }

  /** @param {FakeElement|FakeTextNode} child */
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i === -1) throw new Error('removeChild: not a child');
    this.children.splice(i, 1);
    return child;
  }

  get firstChild() {
    return this.children[0] ?? null;
  }

  /** @param {string} name @param {string} value */
  setAttribute(name, value) {
    this.attrs[name] = value;
    if (name === 'value') this.value = value;
    if (name === 'disabled') this.disabled = true;
  }

  /** @param {string} type @param {(ev: unknown) => unknown} fn */
  addEventListener(type, fn) {
    (this.listeners[type] ??= []).push(fn);
  }

  /**
   * Invoke listeners; returns the array of listener return values so
   * async handlers can be awaited by tests.
   * @param {string} type @param {Record<string, unknown>} [event]
   */
  dispatch(type, event = {}) {
    return (this.listeners[type] ?? []).map((fn) => fn({ target: this, ...event }));
  }

  get textContent() {
    let out = '';
    for (const child of this.children) {
      out += child instanceof FakeTextNode ? child.data : child.textContent;
    }
    return out;
  }

  set textContent(text) {
    this.children = [new FakeTextNode(String(text))];
  }
}

export function makeDocument() {
  return {
    /** @param {string} tag */
    createElement: (tag) => new FakeElement(tag),
    /** @param {string} data */
    createTextNode: (data) => new FakeTextNode(data),
    /** @param {string} _id */
    getElementById: (_id) => null,
  };
}

/** Install the fake document globally; returns a restore function. */
export function installDom() {
  const previous = globalThis.document;
  // @ts-expect-error test double
  globalThis.document = makeDocument();
  return () => {
    // @ts-expect-error test double
    globalThis.document = previous;
  };
}

/**
 * Depth-first search of the fake tree.
 * @param {FakeElement} root
 * @param {(node: FakeElement) => boolean} predicate
 * @returns {FakeElement[]}
 */
export function findAll(root, predicate) {
  /** @type {FakeElement[]} */
  const out = [];
  const walk = (/** @type {FakeElement|FakeTextNode} */ node) => {
    if (node instanceof FakeElement) {
      if (predicate(node)) out.push(node);
      for (const child of node.children) walk(child);
    }
  };
  walk(root);
  return out;
}

/**
 * Find the first button whose text equals the label.
 * @param {FakeElement} root @param {string} label
 */
export function buttonByText(root, label) {
  return findAll(root, (n) => n.tagName === 'BUTTON' && n.textContent === label)[0] ?? null;
}

/** Collect the raw data of every text node under root. */
export function allTextData(/** @type {FakeElement} */ root) {
  /** @type {string[]} */
  const out = [];
  const walk = (/** @type {FakeElement|FakeTextNode} */ node) => {
    if (node instanceof FakeTextNode) out.push(node.data);
    else for (const child of node.children) walk(child);
  };
  walk(root);
  return out;
}

/** Flush pending microtasks + one macrotask so fire-and-forget async settles. */
export async function flush() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}
