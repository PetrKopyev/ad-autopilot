// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { demoLogin } from '../api/setup';
import { SessionProvider } from '../api/session';
import { Layout } from '../components/Layout';
import { LoginPage } from './LoginPage';

vi.mock('../api/setup', async (original) => {
  const actual = await original<typeof import('../api/setup')>();
  return { ...actual, demoLogin: vi.fn(actual.demoLogin) };
});

const fetchMock = vi.fn<typeof fetch>();
function reply(status: number, body: unknown) {
  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); fetchMock.mockReset(); });

async function enterDemo(answer: () => void) {
  vi.stubGlobal('fetch', fetchMock);
  reply(200, { demo: true });
  answer();
  render(<MemoryRouter><SessionProvider><LoginPage /></SessionProvider></MemoryRouter>);
  const demo = await screen.findByRole('button', { name: copy.login.demo });
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(demo);
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

it('вошедшего со страницы входа сразу уводит на главную, в шапке — его вход, а не «Вход»', async () => {
  vi.spyOn(document, 'cookie', 'get').mockReturnValue('__Host-csrf=token');
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url) => new Response(JSON.stringify(new URL(String(url)).pathname === '/api/me'
    ? { id: 1, login: 'owner', email: null, accounts: [] } : { demo: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Routes><Route element={<Layout />}>
    <Route path="login" element={<LoginPage />} /><Route index element={<h1>главная</h1>} />
  </Route></Routes></SessionProvider></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: 'главная' })).toBeTruthy();
  expect(screen.getByText('owner')).toBeTruthy();
  expect(screen.queryByRole('link', { name: copy.login.title })).toBeNull();
});

function renderGuestLogin() {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url) => new Response(JSON.stringify(new URL(String(url)).pathname === '/api/me'
    ? { detail: 'unauthorized' } : { demo: true }), { status: new URL(String(url)).pathname === '/api/me' ? 401 : 200, headers: { 'Content-Type': 'application/json' } }));
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Routes><Route element={<Layout />}>
    <Route path="login" element={<LoginPage />} />
  </Route></Routes></SessionProvider></MemoryRouter>);
}

it('без согласия войти нельзя, с галочкой — можно', async () => {
  renderGuestLogin();
  const demo = await screen.findByRole('button', { name: copy.login.demo });
  const yandex = screen.getByRole('button', { name: copy.login.action }) as HTMLButtonElement;
  expect(yandex.disabled).toBe(true);
  expect((demo as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByText(copy.login.consent, { exact: false }));
  expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
  expect(yandex.disabled).toBe(false);
  expect((demo as HTMLButtonElement).disabled).toBe(false);
});

it('на странице входа одна ссылка на политику и нет «Вход» в шапке', async () => {
  renderGuestLogin();
  await screen.findByRole('button', { name: copy.login.demo });
  const links = screen.getAllByRole('link').filter((link) => link.getAttribute('href') === '/policy');
  expect(links).toHaveLength(1);
  expect(links[0].textContent).toBe(copy.login.policy);
  expect(screen.queryByRole('link', { name: copy.login.title })).toBeNull();
});

it('рядом с входом — что сделает автопилот, тремя пунктами', async () => {
  renderGuestLogin();
  await screen.findByRole('button', { name: copy.login.demo });
  const pitch = screen.getByRole('region', { name: copy.login.pointsLabel });
  expect(pitch.textContent).toContain(copy.login.headline);
  expect(pitch.querySelectorAll('li')).toHaveLength(3);
  expect(screen.getByRole('heading', { level: 1, name: copy.login.title })).toBeTruthy();
});
