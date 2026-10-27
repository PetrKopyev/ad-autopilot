import { createContext, useCallback, useContext, useRef } from 'react';
import type { ReactNode } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router';
import { copy } from '../copy';
import { ApiError, hasSessionCookie } from './client';
import { mockEnabled } from './screens';
import { getMe } from './setup';
import type { Me } from './setup';
import { useResource } from './useResource';

async function loadSession(signal: AbortSignal): Promise<Me | null> {
  // Без cookie сессии сервер ответит 401: не спрашиваем его, иначе в консоли браузера ошибка.
  if (!mockEnabled && !hasSessionCookie()) return null;
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

/**
 * Разделы только для вошедшего: гостя сразу уводим на вход, а страница не шлёт запросов, пока сессия неизвестна.
 * Пока сессия перепроверяется (после подключения кабинета), страница остаётся на месте и не теряет введённое.
 */
export function RequireSession() {
  const session = useSession();
  const signedIn = useRef(false);
  if (session.data === null) { signedIn.current = false; return <Navigate to="/login" replace />; }
  if (session.data) signedIn.current = true;
  if (signedIn.current) return <Outlet />;
  return session.loading ? <p role="status">{copy.screens.loading}</p> : null;
}
