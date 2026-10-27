import { createContext, useCallback, useContext } from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router';
import { ApiError } from './client';
import { getMe } from './setup';
import type { Me } from './setup';
import { useResource } from './useResource';

async function loadSession(signal: AbortSignal): Promise<Me | null> {
  try { return await getMe(signal); }
  catch (reason) {
    if (reason instanceof ApiError && reason.status === 401) return null;
    throw reason;
  }
}
const SessionContext = createContext<ReturnType<typeof useResource<Me | null>> | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  // На странице входа сессии нет: не спрашиваем сервер, иначе в консоли браузера каждый раз ошибка 401.
  const guest = useLocation().pathname === '/login';
  const session = useResource(useCallback((signal: AbortSignal) => guest ? Promise.resolve(null) : loadSession(signal), [guest]));
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

export function useSession() { return useContext(SessionContext)!; }
