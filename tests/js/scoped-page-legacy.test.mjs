import assert from 'node:assert/strict';

const { createScopedPage } = await import('../../sirius_pulse/webui/static/page-context.js');

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.listeners = [];
    this.innerHTML = '';
  }
  querySelector(selector) { return selector === '#save' ? this.save || null : null; }
  querySelectorAll() { return []; }
  addEventListener(type, handler) { this.listeners.push({ type, handler }); }
}

const container = new FakeElement('root');
const save = new FakeElement('save');
container.save = save;
const signal = { aborted: false };
const ctx = {
  container,
  signal,
  isActive: () => !signal.aborted,
  $: (id) => (!signal.aborted && id === 'save' ? save : null),
  $$: () => [],
  timeout: (handler, delay) => (!signal.aborted ? setTimeout(handler, delay) : null),
};
const page = createScopedPage();
page.use(ctx, container);

assert.equal(page.$('save'), save);

signal.aborted = true;
const inert = page.$('save');
assert.notEqual(inert, null, 'aborted legacy lookups should return an inert element');
assert.doesNotThrow(() => inert.addEventListener('click', () => {}));
assert.doesNotThrow(() => { inert.innerHTML = '<p>ignored</p>'; });
assert.equal(save.listeners.length, 0, 'aborted lookups must not mutate old DOM');

let ran = false;
page.timeout(() => { ran = true; }, 0);
await new Promise((resolve) => setTimeout(resolve, 5));
assert.equal(ran, false, 'aborted scoped timers must not run');
