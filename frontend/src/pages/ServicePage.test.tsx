// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { SessionProvider } from '../api/session';
import { ServiceErrorPage, ServicePolicyPage, ServiceVkPage } from './ServicePage';

const fetchMock = vi.fn<typeof fetch>();
let vkOauth = false;
beforeEach(() => {
  vkOauth = false;
  sessionStorage.setItem('ad-autopilot-setup-draft', JSON.stringify({ business: { id: 7 } }));
  vi.spyOn(document, 'cookie', 'get').mockReturnValue('__Host-csrf=token');
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url) => {
    const path = new URL(String(url)).pathname;
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false, vk_oauth: vkOauth },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: true, accounts: [] },
      '/api/accounts/vk/key': { id: 2, business_id: 7, external_login: 'vk', status: 'ok' },
      '/api/business': { id: 9 },
    };
    return new Response(JSON.stringify(answers[path]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); fetchMock.mockReset(); sessionStorage.clear(); });

const renderPage = () => render(<MemoryRouter><SessionProvider><ServiceVkPage /></SessionProvider></MemoryRouter>);
const calls = () => fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname);

it('без приложения VK на сервере — только ключ API, без входа через VK', async () => {
  renderPage();
  expect(await screen.findByRole('heading', { name: copy.launchFlow.keyTitle })).not.toBeNull();
  await waitFor(() => expect(calls()).toContain('/api/auth/config'));
  expect(screen.queryByRole('button', { name: copy.launchFlow.vkOauth })).toBeNull();
  expect(screen.getByText(/ads\.vk\.com/)).not.toBeNull();
});

it('с приложением VK на сервере есть «Войти через VK» с пояснением', async () => {
  vkOauth = true;
  renderPage();
  expect(await screen.findByRole('button', { name: copy.launchFlow.vkOauth })).not.toBeNull();
  expect(screen.getByText(copy.launchFlow.vkOauthHint)).not.toBeNull();
});

it('после подключения по ключу формы скрываются, остаются успех и возврат в мастер', async () => {
  renderPage();
  fireEvent.change(await screen.findByLabelText(copy.launchFlow.clientIdLabel), { target: { value: 'client' } });
  fireEvent.change(screen.getByLabelText(copy.launchFlow.keyLabel), { target: { value: 'secret' } });
  fireEvent.click(screen.getByRole('button', { name: copy.launchFlow.keyConnect }));
  const back = await screen.findByRole('link', { name: copy.launchFlow.backToSetup });
  expect(back.getAttribute('href')).toBe('/setup/2');
  expect(screen.getByRole('status').textContent).toBe(copy.launchFlow.vkConnected);
  expect(screen.queryByLabelText(copy.launchFlow.clientIdLabel)).toBeNull();
  expect(screen.queryByRole('button', { name: copy.launchFlow.keyConnect })).toBeNull();
  expect(calls()).toContain('/api/accounts/vk/key');
});

it('из настроек в новой вкладке: бизнес берётся с сервера, форма доступна, возврат — в настройки', async () => {
  sessionStorage.clear();
  render(<MemoryRouter initialEntries={[{ pathname: '/connect/vk', state: { back: '/settings' } }]}><SessionProvider><ServiceVkPage /></SessionProvider></MemoryRouter>);
  const clientId = await screen.findByLabelText(copy.launchFlow.clientIdLabel) as HTMLInputElement;
  await waitFor(() => expect(clientId.disabled).toBe(false));
  expect(screen.queryByText(copy.launchFlow.needBusiness)).toBeNull();
  fireEvent.change(clientId, { target: { value: 'client' } });
  fireEvent.change(screen.getByLabelText(copy.launchFlow.keyLabel), { target: { value: 'secret' } });
  fireEvent.click(screen.getByRole('button', { name: copy.launchFlow.keyConnect }));
  expect((await screen.findByRole('link', { name: copy.launchFlow.backToSettings })).getAttribute('href')).toBe('/settings');
  const [, init] = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/api/accounts/vk/key'))!;
  expect(JSON.parse(String(init!.body))).toMatchObject({ business_id: 9 });
});

it('ключ API — с подсказкой у поля, без неё поле не описано', async () => {
  renderPage();
  const secret = await screen.findByLabelText(copy.launchFlow.keyLabel);
  expect(document.getElementById(secret.getAttribute('aria-describedby')!)!.textContent).toBe(copy.launchFlow.keyHint);
  expect(screen.getByLabelText(copy.launchFlow.clientIdLabel).getAttribute('aria-describedby')).toBeNull();
});

it('помощь с подключением — три случая, у каждого заголовок и что делать', () => {
  render(<MemoryRouter><ServiceErrorPage /></MemoryRouter>);
  for (const title of [copy.service.reauthTitle, copy.service.lowBalanceTitle, copy.service.unavailableTitle]) {
    expect(screen.getByRole('region', { name: title })).toBeTruthy();
  }
  expect(within(screen.getByRole('region', { name: copy.service.reauthTitle })).getByRole('button', { name: copy.setup.accounts.connectDirect })).toBeTruthy();
  expect(within(screen.getByRole('region', { name: copy.service.lowBalanceTitle })).getByRole('link', { name: copy.launchFlow.openDirect }).getAttribute('href')).toBe('https://direct.yandex.ru/');
});

it('политика: вошедшему — кто вошёл и «Выйти»', async () => {
  render(<MemoryRouter><SessionProvider><ServicePolicyPage /></SessionProvider></MemoryRouter>);
  expect(screen.getByRole('heading', { level: 1, name: copy.service.policyTitle })).toBeTruthy();
  expect(await screen.findByText(copy.launchFlow.signedIn('owner'))).toBeTruthy();
  expect(screen.getByRole('button', { name: copy.launchFlow.logout })).toBeTruthy();
});
