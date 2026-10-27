// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { getAds } from './screens';

const fetchMock = vi.fn<typeof fetch>();
afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); });

it('getAds передаёт период в параметрах from и to, как их ждёт сервер', async () => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockResolvedValueOnce(new Response('{"items":[]}', { status: 200 }));
  await getAds(undefined, { from: '2026-10-21', to: '2026-11-03' });
  const url = new URL(String(fetchMock.mock.calls[0][0]));
  expect(url.pathname).toBe('/api/ads');
  expect(Object.fromEntries(url.searchParams)).toEqual({ from: '2026-10-21', to: '2026-11-03' });
});
