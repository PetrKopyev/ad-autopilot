// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { demoDateKey } from '../api/demo';
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

async function panel(status = idle) {
  reply(200, { demo: true });
  reply(200, status);
  render(<DemoPanel changed={() => {}} />);
  expect(await screen.findByText(`Демо-дата: 27.10.2026. Перемотано ${status.total_days} из 120 дней.`)).toBeTruthy();
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
  reply(200, { ...idle, total_days: 12, advance: { state: 'running', days: 30, done: 12, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: '+1 день' }));
  expect(await screen.findByText('Автопилот обрабатывает прошедшие дни: 12 из 30…')).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  reply(200, { ...idle, today: '2026-11-26', total_days: 30, advance: { state: 'done', days: 30, done: 30, error: '' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(await screen.findByText('Готово. Демо-дата: 26.11.2026. Перемотано 30 из 120 дней.')).toBeTruthy();
  expect(calls()).toEqual(['GET /api/auth/config', 'GET /api/demo/status', 'POST /api/demo/advance', 'GET /api/demo/status', 'GET /api/demo/status']);
});

it('прерванная перемотка досчитывает только оставшиеся дни, и лимит 120 дней не тратится дважды', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  // Уже перемотано 90 дней: запрос на 30 укладывается в лимит ровно.
  const before = { ...idle, total_days: 90 };
  await panel(before);
  reply(202, { ...before, advance: { state: 'running', days: 30, done: 0, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: '+30 дней' }));
  await screen.findByText('Автопилот обрабатывает прошедшие дни: 0 из 30…');
  // Оборвалась на 12-м дне: в лимит засчитаны только пройденные 12 дней.
  reply(200, { ...before, today: '2026-11-08', total_days: 102, advance: { state: 'error', days: 30, done: 12, error: 'Перемотка прервалась. Попробуйте ещё раз.' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect((await screen.findByRole('alert')).textContent).toContain('Перемотка прервалась. Попробуйте ещё раз.');
  expect(screen.getByText('Демо-дата: 08.11.2026. Перемотано 102 из 120 дней.')).toBeTruthy();
  expect((screen.getByRole('button', { name: '+30 дней' }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole('button', { name: '+7 дней' }) as HTMLButtonElement).disabled).toBe(false);
  reply(202, { ...before, today: '2026-11-08', total_days: 102, advance: { state: 'running', days: 18, done: 0, error: '' } });
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  await screen.findByText('Автопилот обрабатывает прошедшие дни: 0 из 18…');
  expect(fetchMock.mock.lastCall![1]!.body).toBe('{"days":18}');
  reply(200, { ...before, today: '2026-11-26', total_days: 120, advance: { state: 'done', days: 18, done: 18, error: '' } });
  await act(async () => { vi.advanceTimersByTime(1000); });
  expect(await screen.findByText('Готово. Демо-дата: 26.11.2026. Перемотано 120 из 120 дней.')).toBeTruthy();
  expect(screen.getAllByRole('button').every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
});

it('после отказа перемотки обновляет разделы и демо-дату, только если дата сменилась', async () => {
  const changed = vi.fn();
  reply(200, { demo: true });
  reply(200, idle);
  render(<DemoPanel changed={changed} />);
  await screen.findByText('Демо-дата: 27.10.2026. Перемотано 0 из 120 дней.');
  expect(changed).toHaveBeenCalledTimes(1);
  // Другая вкладка успела перемотать день: запрос отклонён, но дата уже новая.
  reply(409, { detail: 'Изменение этого бизнеса уже выполняется. Попробуйте позже.' });
  reply(200, { ...idle, today: '2026-10-28', total_days: 1 });
  fireEvent.click(screen.getByRole('button', { name: '+1 день' }));
  expect(await screen.findByText('Демо-дата: 28.10.2026. Перемотано 1 из 120 дней.')).toBeTruthy();
  expect(changed).toHaveBeenCalledTimes(2);
  expect(sessionStorage.getItem(demoDateKey)).toBe('2026-10-28');
  sessionStorage.setItem(demoDateKey, 'прежняя');
  reply(409, { detail: 'Изменение этого бизнеса уже выполняется. Попробуйте позже.' });
  reply(200, { ...idle, today: '2026-10-28', total_days: 1 });
  fireEvent.click(screen.getByRole('button', { name: '+1 день' }));
  await act(async () => {});
  expect(screen.getByRole('alert')).toBeTruthy();
  expect(changed).toHaveBeenCalledTimes(2);
  expect(sessionStorage.getItem(demoDateKey)).toBe('прежняя');
});

it('после отказа не предлагает повтор, который уже не укладывается в 120 дней', async () => {
  await panel({ ...idle, total_days: 110 });
  reply(503, { detail: 'Сервер сейчас занят. Повторите через минуту.' });
  reply(200, { ...idle, total_days: 115 });
  fireEvent.click(screen.getByRole('button', { name: '+7 дней' }));
  expect((await screen.findByRole('alert')).textContent).toContain('Сервер сейчас занят.');
  await screen.findByText('Демо-дата: 27.10.2026. Перемотано 115 из 120 дней.');
  expect(screen.queryByRole('button', { name: copy.common.retry })).toBeNull();
});
