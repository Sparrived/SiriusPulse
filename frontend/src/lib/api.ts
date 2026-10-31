import { getToken, setToken } from './auth';
import { usePersonaStore } from '@/state/persona';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

type Body = BodyInit | Record<string, unknown> | unknown[] | null | undefined;

interface RequestOptions {
  method?: string;
  body?: Body;
  signal?: AbortSignal;
  /** 不带 X-Sirius-Persona（全局接口，或显式按名字操作的接口） */
  global?: boolean;
  raw?: boolean;
}

function isPlainBody(body: Body): body is Record<string, unknown> | unknown[] {
  return (
    body !== null &&
    typeof body === 'object' &&
    !(body instanceof Blob) &&
    !(body instanceof FormData) &&
    !(body instanceof ArrayBuffer) &&
    !(body instanceof URLSearchParams)
  );
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const persona = usePersonaStore.getState().current;
  if (!opts.global && persona) headers['X-Sirius-Persona'] = persona;

  let body: BodyInit | undefined;
  if (isPlainBody(opts.body)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  } else if (opts.body != null) {
    body = opts.body as BodyInit;
  }

  const res = await fetch(path, { method: opts.method ?? 'GET', headers, body, signal: opts.signal });
  if (res.status === 401) {
    setToken(null);
    throw new ApiError('需要重新敲门', 401);
  }
  if (opts.raw) {
    if (!res.ok) throw new ApiError(`请求失败 (${res.status})`, res.status);
    return res as unknown as T;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(String(data?.error ?? `请求失败 (${res.status})`), res.status);
  return data as T;
}

export const api = {
  get: <T>(path: string, opts?: Omit<RequestOptions, 'method' | 'body'>) => request<T>(path, opts),
  post: <T>(path: string, body?: Body, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: Body, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  del: <T>(path: string, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'DELETE' }),
};
