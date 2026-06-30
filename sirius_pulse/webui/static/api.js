const API = '/api';
let authToken = localStorage.getItem('sirius_token') || '';
let activeRequests = 0;
let loadingIndicator = null;
let loadingEventsBound = false;

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

function getLoadingIndicator() {
  if (loadingIndicator) return loadingIndicator;
  loadingIndicator = document.getElementById('globalLoadingIndicator');
  if (!loadingIndicator) {
    loadingIndicator = document.createElement('div');
    loadingIndicator.id = 'globalLoadingIndicator';
    loadingIndicator.className = 'global-loading-indicator';
    loadingIndicator.setAttribute('role', 'status');
    loadingIndicator.setAttribute('aria-live', 'polite');
    loadingIndicator.setAttribute('aria-busy', 'false');
    document.body.appendChild(loadingIndicator);
  }
  return loadingIndicator;
}

function renderLoadingIndicator() {
  const indicator = getLoadingIndicator();
  const loading = activeRequests > 0;
  indicator.textContent = loading
    ? `正在加载数据${activeRequests > 1 ? `（${activeRequests} 个请求）` : ''}…`
    : '';
  indicator.setAttribute('aria-busy', String(loading));
  indicator.classList.toggle('show', loading);
}

function beginLoading() {
  activeRequests += 1;
  renderLoadingIndicator();
}

function endLoading() {
  activeRequests = Math.max(0, activeRequests - 1);
  renderLoadingIndicator();
}

async function withLoading(work) {
  beginLoading();
  try {
    return await work();
  } finally {
    endLoading();
  }
}

function bindLoadingEvents() {
  if (loadingEventsBound || typeof window === 'undefined' || !window.addEventListener) return;
  loadingEventsBound = true;
  window.addEventListener('sirius:loading-begin', beginLoading);
  window.addEventListener('sirius:loading-end', endLoading);
}

bindLoadingEvents();

export async function get(path, signal) {
  return withLoading(async () => {
    const opts = signal ? { signal, headers: authHeaders() } : { headers: authHeaders() };
    let r;
    try { r = await fetch(API + path, opts); } catch (e) { logAndThrow('GET', path, e); }
    handleAuthError(r);
    if (!r.ok) { throw new Error(await readError(r, 'GET', path)); }
    return r.json();
  });
}

export async function post(path, body) {
  return withLoading(async () => {
    let r;
    try { r = await fetch(API + path, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) }); } catch (e) { logAndThrow('POST', path, e); }
    handleAuthError(r);
    if (!r.ok) { throw new Error(await readError(r, 'POST', path)); }
    return r.json();
  });
}

export async function del(path) {
  return withLoading(async () => {
    let r;
    try { r = await fetch(API + path, { method: 'DELETE', headers: authHeaders() }); } catch (e) { logAndThrow('DELETE', path, e); }
    handleAuthError(r);
    if (!r.ok) { throw new Error(await readError(r, 'DELETE', path)); }
    return r.json();
  });
}

export async function put(path, body) {
  return withLoading(async () => {
    let r;
    try { r = await fetch(API + path, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(body) }); } catch (e) { logAndThrow('PUT', path, e); }
    handleAuthError(r);
    if (!r.ok) { throw new Error(await readError(r, 'PUT', path)); }
    return r.json();
  });
}
