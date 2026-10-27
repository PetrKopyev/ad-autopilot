// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { demoLogin } from '../api/setup';
import { LoginPage } from './LoginPage';

vi.mock('../api/setup', async (original) => {
  const actual = await original<typeof import('../api/setup')>();
  return { ...actual, demoLogin: vi.fn(actual.demoLogin) };
});

const fetchMock = vi.fn<typeof fetch>();
function reply(status: number, body: unknown) {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); fetchMock.mockReset(); });

async function enterDemo(answer: () => void) {
  vi.stubGlobal('fetch', fetchMock);
  reply(200, { demo: true });
  answer();
  render(<MemoryRouter><LoginPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Войти в демо' }));
}

it('вход в демо показывает текст сервера, а не «не удалось связаться»', async () => {
  const detail = 'Демо сейчас занято. Попробуйте через несколько часов.';
  await enterDemo(() => reply(503, { detail }));
  expect(await screen.findByText(detail)).toBeTruthy();
  expect(screen.queryByText(copy.errors.network)).toBeNull();
});

it('«не удалось связаться» — только когда сервер не ответил', async () => {
  await enterDemo(() => fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch')));
  expect(await screen.findByText(copy.errors.network)).toBeTruthy();
});

it('503 без текста — общая фраза о сбое сервера', async () => {
  await enterDemo(() => fetchMock.mockResolvedValueOnce(new Response(null, { status: 503 })));
  expect(await screen.findByText(copy.errors.server)).toBeTruthy();
});

it('ответ без текста с необычным кодом — общая фраза об ошибке', async () => {
  await enterDemo(() => fetchMock.mockResolvedValueOnce(new Response(null, { status: 418 })));
  expect(await screen.findByText(copy.errors.generic)).toBeTruthy();
});

it('сбой не в виде ошибки — общая фраза, а не пустое сообщение', async () => {
  await enterDemo(() => vi.mocked(demoLogin).mockRejectedValueOnce('сбой'));
  expect(await screen.findByText(copy.errors.generic)).toBeTruthy();
});
