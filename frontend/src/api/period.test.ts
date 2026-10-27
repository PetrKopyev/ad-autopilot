// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const fetchMock = vi.fn<typeof fetch>();
function reply(body: unknown) {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}
const status = (today: string, now: string) => ({ today, now, total_days: 3, advance: null });
const paths = () => fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname);

beforeEach(() => { vi.resetModules(); sessionStorage.clear(); vi.stubGlobal('fetch', fetchMock); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); fetchMock.mockReset(); });

it('getDemoNow вне демо — null, статус не спрашивает', async () => {
  const { getDemoNow } = await import('./demo');
  reply({ demo: false });
  expect(await getDemoNow()).toBeNull();
  expect(paths()).toEqual(['/api/auth/config']);
});

it('getDemoNow в демо спрашивает время у сервера каждый раз', async () => {
  const { getDemoNow } = await import('./demo');
  reply({ demo: true });
  reply(status('2026-11-26', '2026-11-26T09:00:00+03:00'));
  reply(status('2026-12-03', '2026-12-03T09:00:00+03:00'));
  expect(await getDemoNow()).toBe('2026-11-26T09:00:00+03:00');
  expect(await getDemoNow()).toBe('2026-12-03T09:00:00+03:00');
  expect(paths()).toEqual(['/api/auth/config', '/api/demo/status', '/api/demo/status']);
});

it('getDemoNow после сбоя настройки спрашивает её снова', async () => {
  const { getDemoNow } = await import('./demo');
  fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
  await expect(getDemoNow()).rejects.toThrow();
  reply({ demo: false });
  expect(await getDemoNow()).toBeNull();
});

it('getBusinessToday в демо — демо-дата с сервера, потом из памяти вкладки', async () => {
  const { getBusinessToday } = await import('./period');
  reply({ demo: true });
  reply(status('2026-11-26', '2026-11-26T09:00:00+03:00'));
  expect(await getBusinessToday()).toBe('2026-11-26');
  expect(await getBusinessToday()).toBe('2026-11-26');
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it.each([
  ['2026-10-27T20:59:00Z', '2026-10-27'],
  ['2026-10-27T21:00:00Z', '2026-10-28'],
])('getBusinessToday вне демо считает сутки по Москве: %s → %s', async (now, today) => {
  vi.useFakeTimers({ now: Date.parse(now), toFake: ['Date'] });
  const { getBusinessToday } = await import('./period');
  reply({ demo: false });
  expect(await getBusinessToday()).toBe(today);
});

// Две точки по обе стороны московской полуночи: в любом целом поясе браузера, кроме UTC+3, хотя бы в одной из них его сутки не московские.
it.each([
  ['2026-10-31T20:30:00Z', { from: '2026-10-01', to: '2026-10-31' }],
  ['2026-10-31T21:30:00Z', { from: '2026-11-01', to: '2026-11-01' }],
])('период кончается московским «сегодня», а не сутками браузера: %s', async (now, period) => {
  vi.useFakeTimers({ now: Date.parse(now), toFake: ['Date'] });
  const { getPeriod } = await import('./period');
  reply({ demo: false });
  expect(await getPeriod('month')).toEqual(period);
});

it('в демо период строится от демо-даты', async () => {
  const { getPeriod } = await import('./period');
  reply({ demo: true });
  reply(status('2026-11-26', '2026-11-26T09:00:00+03:00'));
  expect(await getPeriod('month')).toEqual({ from: '2026-11-01', to: '2026-11-26' });
});

it('lastDays берёт дни по сегодняшний включительно', async () => {
  const { lastDays } = await import('./period');
  expect(lastDays(14, '2026-11-03')).toEqual({ from: '2026-10-21', to: '2026-11-03' });
  expect(lastDays(1, '2026-11-03')).toEqual({ from: '2026-11-03', to: '2026-11-03' });
});
