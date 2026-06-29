import assert from 'node:assert/strict';

const { createPageContext } = await import('../../sirius_pulse/webui/static/page-context.js');

class FakeSignal {
  constructor() {
    this.aborted = false;
    this.listeners = [];
  }

  addEventListener(type, listener, options = {}) {
    if (type !== 'abort') return;
    this.listeners.push({ listener, once: Boolean(options.once) });
  }

  removeEventListener(type, listener) {
    if (type !== 'abort') return;
    this.listeners = this.listeners.filter((item) => item.listener !== listener);
  }

  abort() {
    if (this.aborted) return;
    this.aborted = true;
    const listeners = [...this.listeners];
    for (const item of listeners) item.listener();
    this.listeners = this.listeners.filter((item) => !item.once);
  }
}

class FakeElement {
  constructor(id = '', children = {}) {
    this.id = id;
    this.children = children;
    this.events = [];
    this.removed = [];
  }

  querySelector(selector) {
    if (!selector.startsWith('#')) return null;
    return this.children[selector.slice(1)] || null;
  }

  querySelectorAll(selector) {
    if (selector !== '.item') return [];
    return Object.values(this.children).filter((child) => child.className === 'item');
  }

  addEventListener(type, handler, options) {
    this.events.push({ type, handler, options });
  }

  removeEventListener(type, handler, options) {
    this.removed.push({ type, handler, options });
  }
}

const button = new FakeElement('save');
const item = new FakeElement('one');
item.className = 'item';
const outside = new FakeElement('save');
const container = new FakeElement('root', { save: button, one: item });
const signal = new FakeSignal();

globalThis.document = {
  getElementById(id) {
    return id === 'save' ? outside : null;
  },
};

const ctx = createPageContext({ container, signal });

assert.equal(ctx.$('save'), button, 'ctx.$ must query inside the current page container');
assert.deepEqual(ctx.$$('.item'), [item], 'ctx.$$ must query inside the current page container');
assert.equal(ctx.isActive(), true);

let clicked = 0;
ctx.on(ctx.$('save'), 'click', () => { clicked += 1; });
assert.equal(button.events.length, 1);
button.events[0].handler();
assert.equal(clicked, 1);

signal.abort();
assert.equal(ctx.isActive(), false);
assert.equal(button.removed.length, 1, 'abort must remove event listeners for old pages');

ctx.on(ctx.$('save'), 'click', () => { clicked += 1; });
assert.equal(button.events.length, 1, 'aborted contexts must not bind new listeners');
assert.equal(ctx.$('save'), null, 'aborted contexts must stop returning stale DOM');
