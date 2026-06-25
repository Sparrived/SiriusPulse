const state = {
  personas: [],
  currentPersona: null,
  personaState: {},
  providers: [],
  globalConfig: {},
  theme: localStorage.getItem('sirius-theme') || 'dark',
  mode: localStorage.getItem('sirius-mode') || 'butler',
  sidebarCollapsed: false,
  authToken: localStorage.getItem('sirius_token') || '',
};

const listeners = new Map();

export function getState() { return state; }

export function setState(patch) {
  Object.assign(state, patch);
  for (const [key, value] of Object.entries(patch)) {
    const fns = listeners.get(key);
    if (fns) fns.forEach(fn => fn(value));
  }
}

export function subscribe(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  return () => listeners.get(key).delete(fn);
}

export const store = new Proxy(state, {
  get(target, prop) { return target[prop]; },
  set(target, prop, value) {
    target[prop] = value;
    const fns = listeners.get(prop);
    if (fns) fns.forEach(fn => fn(value));
    return true;
  }
});
