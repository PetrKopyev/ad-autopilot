import { copy } from '../copy';

export class ApiError extends Error {
  /** detail — текст ошибки от сервера, если он его прислал. */
  constructor(message: string, public readonly status: number = 0, public readonly detail?: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export interface ApiClientOptions {
  csrfCookieName?: string;
  csrfHeaderName?: string;
}

export type ApiRequestOptions = Omit<RequestInit, 'body' | 'credentials'> & {
  body?: unknown;
};

function readCookie(name: string): string | undefined {
  const prefix = `${encodeURIComponent(name)}=`;
  const cookie = document.cookie.split(';').map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (!cookie) return undefined;
  try {
    return decodeURIComponent(cookie.slice(prefix.length));
  } catch {
    return undefined;
  }
}

function errorMessage(status: number): string {
  switch (status) {
    case 401: return copy.errors.unauthorized;
    case 403: return copy.errors.forbidden;
    case 404: return copy.errors.notFound;
    case 409: return copy.errors.conflict;
    case 400:
    case 422: return copy.errors.validation;
    case 429: return copy.errors.rateLimit;
    default: return status >= 500 ? copy.errors.server : copy.errors.generic;
  }
}

// Сервер объясняет ошибку сам («запуск уже идёт», «файл больше 10 МБ»): его текст понятнее общей фразы.
// Ответы фреймворка и прокси по умолчанию («Not Found») не по-русски — для них остаётся общая фраза.
async function serverMessage(response: Response): Promise<string | undefined> {
  try {
    const { detail } = await response.json() as { detail?: unknown };
    return typeof detail === 'string' && /[а-яё]/i.test(detail) ? detail.trim() : undefined;
  } catch {
    return undefined;
  }
}

export function createApiClient({
  csrfCookieName = '__Host-csrf',
  csrfHeaderName = 'X-CSRF-Token',
}: ApiClientOptions = {}) {
  return async function request<T = unknown>(
    path: string,
    { body, ...options }: ApiRequestOptions = {},
  ): Promise<T | undefined> {
    const url = new URL(path, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith('/api/')) {
      throw new ApiError(copy.errors.unsafeUrl);
    }

    const method = (options.method ?? 'GET').toUpperCase();
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    const form = body instanceof FormData;
    if (body !== undefined && !form) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const csrfToken = readCookie(csrfCookieName);
      if (csrfToken) headers.set(csrfHeaderName, csrfToken);
    }

    let response: Response;
    try {
      response = await fetch(url, {
        ...options,
        method,
        headers,
        credentials: 'include',
        redirect: 'error',
        body: body === undefined || form ? (body as FormData | undefined) : JSON.stringify(body),
      });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new ApiError(copy.errors.network);
    }

    if (!response.ok) {
      const detail = await serverMessage(response);
      throw new ApiError(detail ?? errorMessage(response.status), response.status, detail);
    }
    if (response.status === 204 || response.status === 205 || method === 'HEAD') return undefined;

    const text = await response.text();
    if (!text.trim()) return undefined;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new ApiError(copy.errors.invalidResponse, response.status);
    }
  };
}

export const api = createApiClient();
