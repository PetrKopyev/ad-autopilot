// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { getMe } from './setup';
import { SessionProvider, useSession } from './session';

// Макет живёт без сервера и cookie сессии: вход в нём помнит сам макет.
vi.mock('./screens', () => ({ mockEnabled: true }));
vi.mock('./setup', () => ({ getMe: vi.fn() }));
afterEach(() => { cleanup(); vi.mocked(getMe).mockReset(); });

function Probe() {
  const session = useSession();
  return <p>{session.loading ? 'загрузка' : session.data === null ? 'гость' : session.data?.login}</p>;
}

it('в макете сессию спрашивают и без cookie', async () => {
  vi.mocked(getMe).mockResolvedValue({ id: 1, login: 'макет', email: null, has_business: true, accounts: [] });
  render(<SessionProvider><Probe /></SessionProvider>);
  expect(await screen.findByText('макет')).toBeTruthy();
  expect(getMe).toHaveBeenCalledTimes(1);
});
