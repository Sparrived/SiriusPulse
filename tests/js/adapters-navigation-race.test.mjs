import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/adapters.js', 'utf8')
  .replace(/^import[^\n]+\n/gm, '')
  .replace('export async function init', 'async function init');

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.disabled = false;
    this.listeners = [];
    this._innerHTML = '';
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
  }

  get innerHTML() {
    return this._innerHTML;
  }

  addEventListener(type, listener) {
    this.listeners.push({ type, listener });
  }

  querySelector(selector) {
    if (!selector.startsWith('#')) return null;
    const id = selector.slice(1);
    return this.innerHTML.includes(`id="${id}"`) ? new FakeElement(id) : null;
  }
}

const main = new FakeElement('main');
globalThis.document = {
  getElementById(id) {
    if (id === 'main') return main;
    return main.innerHTML.includes(`id="${id}"`) ? new FakeElement(id) : null;
  },
};

const store = { currentPersona: 'sirius' };
let resolveGet;
const get = () => new Promise((resolve) => { resolveGet = resolve; });
const post = () => { throw new Error('not used'); };
const toast = () => {};
const flashSuccess = () => {};
const $ = (id) => document.getElementById(id);

const { init } = Function('store', 'get', 'post', 'toast', 'flashSuccess', '$', `${source}\nreturn { init };`)(store, get, post, toast, flashSuccess, $);

const initPromise = init(main);
main.innerHTML = '<div id="otherPage"></div>';
resolveGet({ adapters: [{ enabled: true }] });

await assert.doesNotReject(initPromise);
