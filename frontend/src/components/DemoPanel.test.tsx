// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { DemoPanel } from './DemoPanel';

vi.mock('../api/session', () => ({ useSession: () => ({ data: { id: 1, login: 'demo', email: null, accounts: [] } }) }));

const fetchMock = vi.fn<typeof fetch>();
function reply(status: number, body: unknown) {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}
const idle = { today: '2026-10-27', now: '2026-10-27T09:00:00+03:00', total_days: 0, advance: null };
const calls = () => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${new URL(String(url)).pathname}`);

beforeEach(() => { sessionStorage.clear(); vi.stubGlobal('fetch', fetchMock); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); fetchMock.mockReset(); });

async function panel() {
  reply(200, { demo: true });
  reply(200, idle);
  render(<DemoPanel changed={() => {}} />);
  expect(await screen.findByText('Демо-дата: 27.10.2026. Перемотано 0 из 120 дней.')).toBeTruthy();
}

it.each([
  [409, 'Изменение этого бизнеса уже выполняется. Попробуйте позже.'],
  [503, 'Сервер сейчас занят. Повторите через минуту.'],
])('при %i показывает текст сервера и повторяет ту же перемотку', async (status, detail) => {
  await panel();
  reply(status, { detail });
  reply(200, idle);
  fireEvent.click(screen.getByRole('button', { name: '+7 дней' }));
  expect((await screen.findByRole('alert')).textContent).toContain(detail);
  reply(202, { ...idle, advance: { state: 'running', days: 7, done: 0, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  expect(await screen.findByText('Автопилот обрабатывает прошедшие дни: 0 из 7…')).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  const advance = fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
  expect(advance.map(([, init]) => init!.body)).toEqual(['{"days":7}', '{"days":7}']);
});

it('если перемотка уже идёт (брошенная в другой вкладке), показывает её ход, а не «Перемотано 0»', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  await panel();
  reply(409, { detail: 'Идёт перемотка времени. Дождитесь окончания.' });
  reply(200, { ...idle, advance: { state: 'running', days: 30, done: 12, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: '+1 день' }));
  expect(await screen.findByText('Автопилот обрабатывает прошедшие дни: 12 из 30…')).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  reply(200, { ...idle, today: '2026-11-26', total_days: 30, advance: { state: 'done', days: 30, done: 30, error: '' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(await screen.findByText('Готово. Демо-дата: 26.11.2026. Перемотано 30 из 120 дней.')).toBeTruthy();
  expect(calls()).toEqual(['GET /api/auth/config', 'GET /api/demo/status', 'POST /api/demo/advance', 'GET /api/demo/status', 'GET /api/demo/status']);
});

it('прерванная перемотка предлагает досчитать оставшиеся дни', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  await panel();
  reply(202, { ...idle, advance: { state: 'running', days: 30, done: 0, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: '+30 дней' }));
  await screen.findByText('Автопилот обрабатывает прошедшие дни: 0 из 30…');
  reply(200, { ...idle, total_days: 12, advance: { state: 'error', days: 30, done: 12, error: 'Перемотка прервалась. Попробуйте ещё раз.' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect((await screen.findByRole('alert')).textContent).toContain('Перемотка прервалась. Попробуйте ещё раз.');
  reply(202, { ...idle, total_days: 12, advance: { state: 'running', days: 18, done: 0, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  await screen.findByText('Автопилот обрабатывает прошедшие дни: 0 из 18…');
  expect(fetchMock.mock.lastCall![1]!.body).toBe('{"days":18}');
});
