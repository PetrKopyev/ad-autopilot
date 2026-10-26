import { createContext, useContext } from 'react';
import type { ReactNode } from 'react';
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
  const session = useResource(loadSession);
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

export function useSession() { return useContext(SessionContext)!; }
