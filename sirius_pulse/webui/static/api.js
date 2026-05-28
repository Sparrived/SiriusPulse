const API = '/api';
let authToken = localStorage.getItem('sirius_token') || '';

export function setToken(t) { authToken = t; localStorage.setItem('sirius_token', t); }
export function clearToken() { authToken = ''; localStorage.removeItem('sirius_token'); }
export function getToken() { return authToken; }

function authHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (authToken) h['Authorization'] = 'Bearer ' + authToken;
  return h;
}

function handleAuthError(r) {
  if (r.status === 401) {
    clearToken();
    window.dispatchEvent(new CustomEvent('auth:expired'));
    throw new Error('认证已过期');
  }
}

export async function get(path, signal) {
  const opts = signal ? { signal, headers: authHeaders() } : { headers: authHeaders() };
  const r = await fetch(API + path, opts);
  handleAuthError(r);
  if (!r.ok) { const t = await r.text(); throw new Error(`HTTP ${r.status}: ${t.slice(0,200)}`); }
  return r.json();
}

export async function post(path, body) {
  const r = await fetch(API + path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
  handleAuthError(r);
  if (!r.ok) { const t = await r.text(); throw new Error(`HTTP ${r.status}: ${t.slice(0,200)}`); }
  return r.json();
}

export async function del(path) {
  const r = await fetch(API + path, { method: 'DELETE', headers: authHeaders() });
  handleAuthError(r);
  if (!r.ok) { const t = await r.text(); throw new Error(`HTTP ${r.status}: ${t.slice(0,200)}`); }
  return r.json();
}

export async function put(path, body) {
  const r = await fetch(API + path, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(body) });
  handleAuthError(r);
  if (!r.ok) { const t = await r.text(); throw new Error(`HTTP ${r.status}: ${t.slice(0,200)}`); }
  return r.json();
}
