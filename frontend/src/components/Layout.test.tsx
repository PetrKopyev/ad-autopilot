// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { SessionProvider } from '../api/session';
import { Layout } from './Layout';

const fetchMock = vi.fn<typeof fetch>();
let status = 'draft';
beforeEach(() => {
  vi.spyOn(document, 'cookie', 'get').mockReturnValue('__Host-csrf=token');
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url) => {
    const path = new URL(String(url)).pathname;
    const answers: Record<string, unknown> = {
      '/api/me': { id: 1, login: 'owner', email: null, has_business: true, accounts: [] },
      '/api/autopilot/status': { status, last_check_at: null, auto_continue: true, needs_attention: [] },
    };
    return new Response(JSON.stringify(answers[path]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); fetchMock.mockReset(); status = 'draft'; });

function renderAt(path: string) {
  render(<MemoryRouter initialEntries={[path]}><SessionProvider><Routes><Route element={<Layout />}>
    <Route index element={<h1>главная</h1>} /><Route path="plan" element={<h1>план</h1>} /><Route path="setup/:step" element={<h1>мастер</h1>} />
  </Route></Routes></SessionProvider></MemoryRouter>);
}
const calls = () => fetchMock.mock.calls.map(([url]) => new URL(String(url)).pathname);

it.each(['/setup/2', '/plan'])('до запуска на %s вместо меню — подпись', async (path) => {
  renderAt(path);
  expect(await screen.findByText(copy.navigation.locked)).toBeTruthy();
  await waitFor(() => expect(calls()).toContain('/api/autopilot/status'));
  expect(screen.queryByRole('navigation')).toBeNull();
});

it.each(['running', 'paused', 'budget_stop'])('запустившийся (%s) видит меню и в мастере', async (value) => {
  status = value;
  renderAt('/setup/2');
  expect(await screen.findByRole('navigation', { name: copy.navigation.label })).toBeTruthy();
  expect(screen.queryByText(copy.navigation.locked)).toBeNull();
});

it('вне мастера и плана меню открыто без вопроса о запуске', async () => {
  renderAt('/');
  expect(await screen.findByRole('navigation', { name: copy.navigation.label })).toBeTruthy();
  expect(calls()).toEqual(['/api/me']);
});
