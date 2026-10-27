// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
  answers = { '/api/auth/config': { demo: true }, '/api/me': { id: 1, login: 'demo', email: null, accounts: [] } };
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
const noBusiness = { status: 'no_business', last_check_at: null, auto_continue: false, needs_attention: [] };

it('после «Войти в демо» ведёт в мастер, а не на главную', async () => {
  answers['/api/autopilot/status'] = noBusiness;
  app('/login');
  fireEvent.click(await screen.findByRole('button', { name: 'Войти в демо' }));
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
  expect(opened.mock.calls).toEqual([['мастер']]);
  expect(calls()).toEqual(['GET /api/auth/config', 'POST /api/auth/demo', 'GET /api/me']);
});

it.each(['/', '/ads'])('без бизнеса %s сразу ведёт в мастер, и раздел не открывается', async (path) => {
  cookie = '__Host-csrf=token';
  answers['/api/autopilot/status'] = noBusiness;
  app(path);
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
  expect(opened.mock.calls).toEqual([['мастер']]);
  expect(calls()).toEqual(['GET /api/me', 'GET /api/autopilot/status']);
});

it('вошедший без бизнеса со страницы входа попадает в мастер', async () => {
  cookie = '__Host-csrf=token';
  answers['/api/autopilot/status'] = noBusiness;
  app('/login');
  expect(await screen.findByRole('heading', { name: 'мастер' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/setup');
});

it('с бизнесом открывает главную', async () => {
  cookie = '__Host-csrf=token';
  answers['/api/autopilot/status'] = { ...noBusiness, status: 'running' };
  app('/');
  expect(await screen.findByRole('heading', { name: 'главная' })).toBeTruthy();
  expect(screen.getByTestId('path').textContent).toBe('/');
});

it('сбой при проверке бизнеса показывает ошибку с повтором и не открывает раздел', async () => {
  cookie = '__Host-csrf=token';
  fetchMock.mockImplementationOnce(async () => new Response(JSON.stringify(answers['/api/me']), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  fetchMock.mockImplementationOnce(async () => new Response(JSON.stringify({ detail: 'Сервис временно недоступен.' }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
  answers['/api/autopilot/status'] = { ...noBusiness, status: 'running' };
  app('/');
  expect((await screen.findByRole('alert')).textContent).toContain('Сервис временно недоступен.');
  expect(opened).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  expect(await screen.findByRole('heading', { name: 'главная' })).toBeTruthy();
});

it('несуществующий адрес показывает «Страница не найдена» со ссылкой на главную', async () => {
  app('/nonexistent');
  expect(screen.getByText(copy.service.notFoundTitle)).toBeTruthy();
  expect(screen.getByText(copy.service.notFoundDescription)).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.service.goHome }).getAttribute('href')).toBe('/');
});
