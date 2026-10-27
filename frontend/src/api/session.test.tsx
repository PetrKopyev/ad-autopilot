// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, useNavigate } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { getMe } from './setup';
import { SessionProvider, useSession } from './session';

vi.mock('./setup', () => ({ getMe: vi.fn() }));

afterEach(() => { cleanup(); vi.mocked(getMe).mockReset(); });

let go: (path: string) => void = () => {};
function Probe() {
  const session = useSession();
  go = useNavigate();
  return <p>{session.loading ? 'загрузка' : session.data === null ? 'гость' : session.data?.login}</p>;
}
const me = { id: 1, login: 'owner', email: null, accounts: [] };

it('на странице входа не спрашивает сервер о сессии, а после ухода с неё спрашивает', async () => {
  vi.mocked(getMe).mockResolvedValue(me);
  render(<MemoryRouter initialEntries={['/login']}><SessionProvider><Probe /></SessionProvider></MemoryRouter>);
  expect(await screen.findByText('гость')).toBeTruthy();
  expect(getMe).not.toHaveBeenCalled();
  act(() => go('/setup'));
  expect(await screen.findByText('owner')).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
});
