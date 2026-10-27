import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { ApiError, createApiClient } from './client';

const request = createApiClient();
const fetchMock = vi.fn<typeof fetch>();

function reply(status: number, body: unknown, type = 'application/json') {
  fetchMock.mockResolvedValueOnce(new Response(typeof body === 'string' ? body : JSON.stringify(body), { status, headers: { 'Content-Type': type } }));
}
async function failure(path = '/api/plan/launch'): Promise<ApiError> {
  const error = await request(path, { method: 'POST' }).catch((reason: unknown) => reason);
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

beforeEach(() => {
  vi.stubGlobal('window', { location: { origin: 'https://app.example' } });
  vi.stubGlobal('document', { cookie: '__Host-csrf=token%2F1' });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); });

describe('ошибки сервера', () => {
  it.each([409, 413, 422, 429, 502])('показывает текст сервера при %i', async (status) => {
    reply(status, { detail: 'Запуск уже идёт. Дождитесь окончания.' });
    const error = await failure();
    expect(error.message).toBe('Запуск уже идёт. Дождитесь окончания.');
    expect(error.status).toBe(status);
    expect(error.detail).toBe('Запуск уже идёт. Дождитесь окончания.');
  });

  it('без текста сервера показывает общую фразу по статусу', async () => {
    reply(429, {});
    expect((await failure()).message).toBe(copy.errors.rateLimit);
    reply(502, '<html>Bad Gateway</html>', 'text/html');
    expect((await failure()).message).toBe(copy.errors.server);
  });

  it('не показывает служебные ответы фреймворка и ошибки проверки полей', async () => {
    reply(404, { detail: 'Not Found' });
    const notFound = await failure();
    expect(notFound.message).toBe(copy.errors.notFound);
    expect(notFound.detail).toBeUndefined();
    reply(422, { detail: [{ loc: ['body', 'days'], msg: 'Input should be less than 36' }] });
    expect((await failure()).message).toBe(copy.errors.validation);
  });

  it('сетевой сбой — понятная фраза', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    expect((await failure()).message).toBe(copy.errors.network);
  });
});

describe('запросы', () => {
  it('изменения отправляет с CSRF-токеном и JSON', async () => {
    reply(200, { ok: true });
    expect(await request('/api/notifications/7/read', { method: 'POST', body: { a: 1 } })).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('https://app.example/api/notifications/7/read');
    const headers = init!.headers as Headers;
    expect(headers.get('X-CSRF-Token')).toBe('token/1');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(init!.body).toBe('{"a":1}');
  });

  it('чтение без CSRF-токена', async () => {
    reply(200, []);
    await request('/api/notifications');
    expect((fetchMock.mock.calls[0][1]!.headers as Headers).has('X-CSRF-Token')).toBe(false);
  });

  it('не уходит за пределы /api/ своего сайта', async () => {
    await expect(request('https://evil.example/api/me')).rejects.toThrow(copy.errors.unsafeUrl);
    await expect(request('/login')).rejects.toThrow(copy.errors.unsafeUrl);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
