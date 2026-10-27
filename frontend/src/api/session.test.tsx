// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { useEffect } from 'react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import { getMe } from './setup';
import { RequireBusiness, RequireSession, SessionProvider, useSession } from './session';

vi.mock('./setup', () => ({ getMe: vi.fn() }));

const cookie = vi.fn(() => '__Host-csrf=token');
beforeEach(() => { vi.spyOn(document, 'cookie', 'get').mockImplementation(cookie); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.mocked(getMe).mockReset(); cookie.mockReset().mockImplementation(() => '__Host-csrf=token'); });

let go: (path: string) => void = () => {};
let session: ReturnType<typeof useSession>;
function Probe() {
  session = useSession();
  go = useNavigate();
  return <p>{session.loading ? 'загрузка' : session.data === null ? 'гость' : session.data?.login}</p>;
}
const me = { id: 1, login: 'owner', email: null, has_business: true, accounts: [] };

it('на странице входа без cookie сессии не спрашивает сервер', async () => {
  cookie.mockImplementation(() => '');
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Probe /></SessionProvider></MemoryRouter>);
  expect(await screen.findByText('гость')).toBeTruthy();
  expect(getMe).not.toHaveBeenCalled();
});

it('вошедший на странице входа остаётся вошедшим', async () => {
  vi.mocked(getMe).mockResolvedValue(me);
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Probe /></SessionProvider></MemoryRouter>);
  expect(await screen.findByText('owner')).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
});

it('после демо-входа сессия перечитывается ещё на странице входа и не остаётся гостевой', async () => {
  cookie.mockImplementation(() => '');
  vi.mocked(getMe).mockResolvedValue(me);
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Probe /></SessionProvider></MemoryRouter>);
  expect(await screen.findByText('гость')).toBeTruthy();
  cookie.mockImplementation(() => '__Host-csrf=token');
  act(() => session.reload());
  expect(await screen.findByText('owner')).toBeTruthy();
  act(() => go('/setup'));
  expect(screen.getByText('owner')).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
});

// Страница раздела: её запросы к серверу стартуют при появлении на экране.
const load = vi.fn();
function Section() {
  useEffect(() => { load(); }, []);
  return <p>раздел</p>;
}
function app(path = '/') {
  render(<MemoryRouter initialEntries={[path]}><SessionProvider><Probe /><Routes>
    <Route path="login" element={<h1>вход</h1>} />
    <Route element={<RequireSession />}><Route index element={<Section />} /><Route path="ads" element={<Section />} /></Route>
  </Routes></SessionProvider></MemoryRouter>);
}
afterEach(() => load.mockReset());

it('без cookie сессии сразу уводит на вход и не шлёт ни одного запроса', async () => {
  cookie.mockImplementation(() => '');
  app('/ads');
  expect(await screen.findByRole('heading', { name: 'вход' })).toBeTruthy();
  expect(getMe).not.toHaveBeenCalled();
  expect(load).not.toHaveBeenCalled();
});

it('сессия истекла: один вопрос о сессии, потом вход; запросы раздела не стартуют', async () => {
  vi.mocked(getMe).mockRejectedValue(new ApiError('Войдите заново, чтобы продолжить.', 401));
  app();
  expect(screen.getByRole('status').textContent).toBe('Загружаем данные…');
  expect(await screen.findByRole('heading', { name: 'вход' })).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
  expect(load).not.toHaveBeenCalled();
});

it('сбой сервера при вопросе о сессии — не уводит на вход и не открывает раздел', async () => {
  vi.mocked(getMe).mockRejectedValue(new ApiError('Сервис временно недоступен.', 503));
  app();
  await act(async () => {});
  expect(screen.queryByRole('heading', { name: 'вход' })).toBeNull();
  expect(screen.queryByText('раздел')).toBeNull();
  expect(load).not.toHaveBeenCalled();
});

it('вошедшему открывает раздел, а перепроверка сессии его не пересоздаёт', async () => {
  vi.mocked(getMe).mockResolvedValue(me);
  app();
  expect(await screen.findByText('раздел')).toBeTruthy();
  let answer: (value: typeof me) => void = () => {};
  vi.mocked(getMe).mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
  act(() => session.reload());
  expect(screen.getByText('загрузка')).toBeTruthy();
  expect(screen.getByText('раздел')).toBeTruthy();
  await act(async () => answer(me));
  expect(load).toHaveBeenCalledTimes(1);
});

it('после выхода уводит на вход', async () => {
  vi.mocked(getMe).mockResolvedValue(me);
  app();
  expect(await screen.findByText('раздел')).toBeTruthy();
  cookie.mockImplementation(() => '');
  act(() => session.reload());
  expect(await screen.findByRole('heading', { name: 'вход' })).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
});

function businessApp(path = '/') {
  render(<MemoryRouter initialEntries={[path]}><SessionProvider><Probe /><Routes>
    <Route path="login" element={<h1>вход</h1>} />
    <Route element={<RequireSession />}>
      <Route path="setup" element={<h1>мастер</h1>} />
      <Route element={<RequireBusiness />}><Route index element={<Section />} /></Route>
    </Route>
  </Routes></SessionProvider></MemoryRouter>);
}

it('без бизнеса раздел ведёт в мастер по ответу о сессии, без своего запроса', async () => {
  vi.mocked(getMe).mockResolvedValue({ ...me, has_business: false });
  businessApp();
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
  expect(load).not.toHaveBeenCalled();
});

it('раздел бизнеса не пересоздаётся, пока сессия перепроверяется, и уводит в мастер, когда бизнеса не стало', async () => {
  vi.mocked(getMe).mockResolvedValue(me);
  businessApp();
  expect(await screen.findByText('раздел')).toBeTruthy();
  let answer: (value: typeof me) => void = () => {};
  vi.mocked(getMe).mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
  act(() => session.reload());
  expect(screen.getByText('раздел')).toBeTruthy();
  await act(async () => answer(me));
  expect(load).toHaveBeenCalledTimes(1);
  vi.mocked(getMe).mockResolvedValue({ ...me, has_business: false });
  act(() => session.reload());
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
});
