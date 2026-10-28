// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { SessionProvider } from '../api/session';
import { ServiceVkPage } from './ServicePage';

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
