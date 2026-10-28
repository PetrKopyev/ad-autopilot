// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { MemoryRouter, useLocation } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';
import { copy } from './copy';

// Страницы разделов заменены метками: проверяем, куда ведёт приложение и какие разделы успели открыться.
const opened = vi.fn();
function Section({ name }: { name: string }) {
  useEffect(() => { opened(name); }, [name]);
  return <h1>{name}</h1>;
}
vi.mock('./pages/SetupPage', () => ({ SetupPage: () => <Section name="мастер" /> }));
vi.mock('./pages/HomePage', () => ({ HomePage: () => <Section name="главная" /> }));
vi.mock('./pages/AdsPage', () => ({ AdsPage: () => <Section name="реклама" /> }));

const fetchMock = vi.fn<typeof fetch>();
let cookie = '';
let answers: Record<string, unknown> = {};
beforeEach(() => {
  cookie = '';
  answers = { '/api/auth/config': { demo: true }, '/api/me': { id: 1, login: 'demo', email: null, has_business: false, accounts: [] } };
  vi.spyOn(document, 'cookie', 'get').mockImplementation(() => cookie);
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (init?.method === 'POST' && path === '/api/auth/demo') { cookie = '__Host-csrf=token'; return new Response(null, { status: 204 }); }
    return new Response(JSON.stringify(answers[path]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); fetchMock.mockReset(); opened.mockReset(); });

function Location() { return <p data-testid="path">{useLocation().pathname}</p>; }
function app(path: string) {
  render(<MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter>);
}
const calls = () => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${new URL(String(url)).pathname}`);
const withBusiness = () => { answers['/api/me'] = { ...(answers['/api/me'] as object), has_business: true }; };

it('после «Войти в демо» ведёт в мастер, а не на главную', async () => {
  app('/login');
  fireEvent.click(await screen.findByRole('button', { name: 'Войти в демо' }));
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
  await waitFor(() => expect(opened.mock.calls).toEqual([['мастер']]));
  expect(calls()).toEqual(['GET /api/auth/config', 'POST /api/auth/demo', 'GET /api/me']);
});

it.each(['/', '/ads'])('без бизнеса %s сразу ведёт в мастер, и раздел не открывается', async (path) => {
  cookie = '__Host-csrf=token';
  app(path);
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
  await waitFor(() => expect(opened.mock.calls).toEqual([['мастер']]));
  expect(calls()).toEqual(['GET /api/me']);
});

it('вошедший без бизнеса со страницы входа попадает в мастер', async () => {
  cookie = '__Host-csrf=token';
  app('/login');
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
});

it.each([['/', 'главная'], ['/ads', 'реклама']])('с бизнесом %s открывается без запроса статуса автопилота', async (path, name) => {
  cookie = '__Host-csrf=token';
  withBusiness();
  app(path);
  expect(await screen.findByRole('heading', { name })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe(path);
  expect(calls()).toEqual(['GET /api/me']);
});

it('сбой при вопросе о сессии показывает ошибку с повтором и не открывает раздел', async () => {
  cookie = '__Host-csrf=token';
  withBusiness();
  fetchMock.mockImplementationOnce(async () => new Response(JSON.stringify({ detail: 'Сервис временно недоступен.' }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
  app('/');
  expect((await screen.findByRole('alert')).textContent).toContain('Сервис временно недоступен.');
  expect(opened).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  expect(await screen.findByRole('heading', { name: 'главная' })).toBeTruthy();
});

it('после удаления данных вход показывает итог остановки рекламы, а не уводит на главную; после входа ведёт в мастер', async () => {
  cookie = '__Host-csrf=token';
  withBusiness();
  answers['/api/autopilot/status'] = { status: 'running', last_check_at: null, auto_continue: true, needs_attention: [] };
  answers['/api/business'] = { id: 1, site_url: 'https://example.ru', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null,
    auto_continue: true, description: null, marking: null };
  const message = 'Данные удалены, но не все площадки подтвердили остановку рекламы.';
  const answer = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation(async (url, init) => {
    if (init?.method !== 'DELETE') return answer(url, init);
    cookie = '';
    return new Response(JSON.stringify({ stopped: false, message }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  app('/settings');
  fireEvent.change(await screen.findByRole('textbox', { name: copy.settingsScreen.deleteLabel }), { target: { value: copy.settingsScreen.deleteWord } });
  fireEvent.click(screen.getByRole('button', { name: copy.settingsScreen.deleteButton }));
  expect((await screen.findByRole('alert')).textContent).toBe(message);
  expect(screen.getByTestId('path').textContent).toBe('/login');
  expect(screen.getByRole('heading', { name: copy.login.title })).toBeTruthy();
  // Страница входа перечитывает сессию сама: в шапке — «Вход», а итог остановки остаётся на месте.
  expect(await screen.findByRole('link', { name: copy.login.title })).toBeTruthy();
  expect(screen.queryByText('demo')).toBeNull();
  expect(screen.getByRole('alert').textContent).toBe(message);
  expect(screen.getByTestId('path').textContent).toBe('/login');
  answers['/api/me'] = { id: 2, login: 'demo-2', email: null, has_business: false, accounts: [] };
  fireEvent.click(await screen.findByRole('button', { name: 'Войти в демо' }));
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
});

it('вошедшего, которого «назад» вернуло на вход с прежним итогом остановки, уводит в раздел', async () => {
  cookie = '__Host-csrf=token';
  withBusiness();
  const stopResult = { stopped: true, message: 'Данные удалены. Рекламу перед удалением остановили.' };
  render(<MemoryRouter initialEntries={[{ pathname: '/login', state: { stopResult } }]}><App /><Location /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: 'главная' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/');
  expect(screen.queryByText(stopResult.message)).toBeNull();
});

it('гостя из раздела сразу уводит на вход, и раздел не шлёт запросов', async () => {
  app('/ads');
  expect(await screen.findByRole('heading', { name: copy.login.title })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/login');
  expect(opened).not.toHaveBeenCalled();
  expect(calls()).toEqual(['GET /api/auth/config']);
});

it('политика открывается гостю, а меню и служебные ссылки — только вошедшему', async () => {
  app('/policy');
  expect(screen.getByRole('heading', { name: copy.service.policyTitle })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/policy');
  expect(screen.queryByRole('navigation')).toBeNull();
  expect(screen.queryByRole('link', { name: copy.service.errorsTitle })).toBeNull();
  expect(screen.queryByRole('link', { name: copy.setup.accounts.vk })).toBeNull();
  expect(calls()).toEqual([]);
});

it('несуществующий адрес показывает «Страница не найдена» со ссылкой на главную', async () => {
  app('/nonexistent');
  expect(screen.getByText(copy.service.notFoundTitle)).toBeTruthy();
  expect(screen.getByText(copy.service.notFoundDescription)).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.service.goHome }).getAttribute('href')).toBe('/');
});
