// 与旧版控制台共用同一个 localStorage 键：迁移期两边登录态互通。
const TOKEN_KEY = 'sirius_token';

type Listener = (token: string | null) => void;
const listeners = new Set<Listener>();

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  listeners.forEach((fn) => fn(token));
}

export function onTokenChange(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function knock(password: string): Promise<{ ok: true } | { ok: false; reason: string }> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password }),
  }).catch(() => null);
  if (!res) return { ok: false, reason: '屋里好像没有人应门……服务还没启动吗？' };
  const data = await res.json().catch(() => ({}));
  if (res.ok && data.token) {
    setToken(data.token);
    return { ok: true };
  }
  return { ok: false, reason: '好像不是这个暗号呢' };
}
