import { api, ApiError } from './client';
import { copy } from '../copy';

/** days — сколько дней запрошено, done — сколько уже полностью пройдено. */
export interface DemoAdvance { state: 'running' | 'done' | 'error'; days: number; done: number; error: string }
/** total_days — сколько дней уже перемотано из 120, вместе с пройденными днями идущей перемотки. */
export interface DemoStatus { today: string; now: string; total_days: number; advance: DemoAdvance | null; has_business: boolean }
export const demoDateKey = 'ad-autopilot-demo-date';

let demoMode: Promise<boolean> | undefined;

export function rememberDemoDate(status: DemoStatus) {
  try { sessionStorage.setItem(demoDateKey, status.today); } catch { /* Continue without storage. */ }
}
export async function getDemoStatus(signal?: AbortSignal): Promise<DemoStatus> {
  const result = await api<DemoStatus>('/api/demo/status', { signal });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function advanceDemo(days: number): Promise<DemoStatus> {
  const result = await api<DemoStatus>('/api/demo/advance', { method: 'POST', body: { days } });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
function isDemo(): Promise<boolean> {
  demoMode ??= api<{ demo: boolean }>('/api/auth/config').then((config) => config?.demo === true)
    .catch((error: unknown) => { demoMode = undefined; throw error; });
  return demoMode;
}
/** Демо-дата бизнеса; вне демо — null. В демо окно периода строится только после ответа статуса. */
export async function getDemoDate(signal?: AbortSignal): Promise<string | null> {
  try { const stored = sessionStorage.getItem(demoDateKey); if (stored) return stored; } catch { /* Ask the server. */ }
  if (!(await isDemo())) return null;
  const status = await getDemoStatus(signal);
  rememberDemoDate(status);
  return status.today;
}
/** Точное время демо-бизнеса; вне демо — null. После перемотки оно стоит на месте, поэтому спрашиваем сервер каждый раз. */
export async function getDemoNow(signal?: AbortSignal): Promise<string | null> {
  if (!(await isDemo())) return null;
  return (await getDemoStatus(signal)).now;
}
