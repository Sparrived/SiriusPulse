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
  /** 指定作用的人格（默认是这个标签页正在看的那位） */
  persona?: string;
  raw?: boolean;
  /** 原样透传的请求头，如上传图片时的 Content-Type */
  headers?: Record<string, string>;
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
  const headers: Record<string, string> = { ...opts.headers };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const persona = opts.persona ?? usePersonaStore.getState().current;
  // 请求头只能是 Latin-1，人格目录名却可以是中文：统一做 URL 编码，服务端解码
  if (!opts.global && persona) headers['X-Sirius-Persona'] = encodeURIComponent(persona);

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
