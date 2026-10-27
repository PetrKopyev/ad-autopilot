// @vitest-environment jsdom
import { StrictMode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getDemoNow } from './demo';
import { useBusinessNow } from './useBusinessNow';

vi.mock('./demo', () => ({ getDemoNow: vi.fn() }));

const real = Date.parse('2026-10-27T10:00:00Z');
const day = 86_400_000;
const answer = (time: number) => vi.mocked(getDemoNow).mockResolvedValueOnce(new Date(time).toISOString());
const settle = () => act(() => vi.advanceTimersByTimeAsync(0));
const tick = () => act(() => vi.advanceTimersByTimeAsync(30_000));

beforeEach(() => { vi.useFakeTimers({ now: real }); });
afterEach(() => { vi.useRealTimers(); vi.mocked(getDemoNow).mockReset(); });

it('пока сервер не ответил, время неизвестно, а потом идёт демо-время', async () => {
  let resolve: (value: string) => void = () => {};
  vi.mocked(getDemoNow).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const { result } = renderHook(() => useBusinessNow());
  expect(result.current).toBeNull();
  resolve(new Date(real + 30 * day).toISOString());
  await settle();
  expect(result.current).toBe(real + 30 * day);
});

it('сверяется с сервером каждые 30 секунд и берёт новый сдвиг после перемотки', async () => {
  answer(real + day);
  const { result } = renderHook(() => useBusinessNow());
  await settle();
  expect(result.current).toBe(real + day);
  answer(real + 30_000 + 8 * day);
  await tick();
  expect(getDemoNow).toHaveBeenCalledTimes(2);
  expect(result.current).toBe(real + 30_000 + 8 * day);
});

it('если демо-сервер не ответил, оставляет прежний сдвиг, а часы идут', async () => {
  answer(real + day);
  const { result } = renderHook(() => useBusinessNow());
  await settle();
  vi.mocked(getDemoNow).mockRejectedValueOnce(new Error('сеть'));
  await tick();
  expect(result.current).toBe(real + 30_000 + day);
});

it('без первого ответа считает время настоящим, а не неизвестным', async () => {
  vi.mocked(getDemoNow).mockRejectedValueOnce(new Error('сеть'));
  const { result } = renderHook(() => useBusinessNow());
  await settle();
  expect(result.current).toBe(real);
});

it('вне демо время настоящее', async () => {
  vi.mocked(getDemoNow).mockResolvedValueOnce(null);
  const { result } = renderHook(() => useBusinessNow());
  await settle();
  expect(result.current).toBe(real);
  vi.mocked(getDemoNow).mockResolvedValueOnce(null);
  await tick();
  expect(result.current).toBe(real + 30_000);
});

it('после ухода со страницы отменяет запрос и больше не сверяется', async () => {
  let resolve: (value: string) => void = () => {};
  vi.mocked(getDemoNow).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const { result, unmount } = renderHook(() => useBusinessNow());
  const signal = vi.mocked(getDemoNow).mock.calls[0][0]!;
  unmount();
  expect(signal.aborted).toBe(true);
  resolve(new Date(real + day).toISOString());
  await tick();
  expect(result.current).toBeNull();
  expect(getDemoNow).toHaveBeenCalledTimes(1);
});

it('ответ на отменённую сверку не перезаписывает свежий сдвиг', async () => {
  let resolveStale: (value: string) => void = () => {};
  vi.mocked(getDemoNow).mockReturnValueOnce(new Promise((done) => { resolveStale = done; }));
  answer(real + 8 * day);
  const { result } = renderHook(() => useBusinessNow(), { wrapper: StrictMode });
  await settle();
  resolveStale(new Date(real + day).toISOString());
  await settle();
  expect(vi.mocked(getDemoNow).mock.calls[0][0]!.aborted).toBe(true);
  expect(result.current).toBe(real + 8 * day);
});
