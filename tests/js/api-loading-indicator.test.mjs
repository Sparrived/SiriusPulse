import assert from 'node:assert/strict';

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : Boolean(force);
    if (enabled) this.add(value);
    else this.remove(value);
    return enabled;
  }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(tag = 'div') {
    this.tag = tag;
    this.id = '';
    this.className = '';
    this.classList = new FakeClassList();
    this.children = [];
    this.attributes = {};
    this.textContent = '';
    this.innerHTML = '';
  }
  appendChild(child) {
    this.children.push(child);
    if (child.id) document.elements.set(child.id, child);
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
}

globalThis.localStorage = {
  data: new Map(),
  getItem(key) { return this.data.get(key) || null; },
  setItem(key, value) { this.data.set(key, String(value)); },
  removeItem(key) { this.data.delete(key); },
};

globalThis.document = {
  elements: new Map(),
  body: new FakeElement('body'),
  createElement(tag) { return new FakeElement(tag); },
  getElementById(id) { return this.elements.get(id) || null; },
};

globalThis.window = {
  dispatchEvent() {},
};
globalThis.CustomEvent = class CustomEvent { constructor(type) { this.type = type; } };

let resolveFetch;
globalThis.fetch = () => new Promise((resolve) => { resolveFetch = resolve; });

const { get } = await import(`../../sirius_pulse/webui/static/api.js?loading=${Date.now()}`);
const request = get('/slow');

await Promise.resolve();
const indicator = document.getElementById('globalLoadingIndicator');
assert.ok(indicator, 'API requests should create a global loading indicator');
assert.equal(indicator.getAttribute('aria-busy'), 'true');
assert.match(indicator.textContent, /正在加载数据/);
assert.equal(indicator.classList.contains('show'), true);

resolveFetch({ ok: true, status: 200, json: async () => ({ ok: true }) });
assert.deepEqual(await request, { ok: true });
await Promise.resolve();
assert.equal(indicator.classList.contains('show'), false, 'loading indicator should hide after request settles');
assert.equal(indicator.getAttribute('aria-busy'), 'false');
