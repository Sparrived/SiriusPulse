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

async function readError(r, method, path) {
  let body = '';
  try { body = await r.text(); } catch {}
  const msg = `HTTP ${r.status}: ${body.slice(0, 200)}`;
  console.error(`[API ${method}] ${path} → ${msg}`);
  return msg;
}

function logAndThrow(method, path, err) {
  console.error(`[API ${method}] ${path} → 网络错误:`, err);
  throw err;
}

export async function get(path, signal) {
  const opts = signal ? { signal, headers: authHeaders() } : { headers: authHeaders() };
  let r;
  try { r = await fetch(API + path, opts); } catch (e) { logAndThrow('GET', path, e); }
  handleAuthError(r);
  if (!r.ok) { throw new Error(await readError(r, 'GET', path)); }
  return r.json();
}

export async function post(path, body) {
  let r;
  try { r = await fetch(API + path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) }); } catch (e) { logAndThrow('POST', path, e); }
  handleAuthError(r);
  if (!r.ok) { throw new Error(await readError(r, 'POST', path)); }
  return r.json();
}

export async function del(path) {
  let r;
  try { r = await fetch(API + path, { method: 'DELETE', headers: authHeaders() }); } catch (e) { logAndThrow('DELETE', path, e); }
  handleAuthError(r);
  if (!r.ok) { throw new Error(await readError(r, 'DELETE', path)); }
  return r.json();
}

export async function put(path, body) {
  let r;
  try { r = await fetch(API + path, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(body) }); } catch (e) { logAndThrow('PUT', path, e); }
  handleAuthError(r);
  if (!r.ok) { throw new Error(await readError(r, 'PUT', path)); }
  return r.json();
}
