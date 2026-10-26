import { api, ApiError } from './client';
import { copy } from '../copy';

export interface DemoStatus { today: string; total_days: number }
export const demoDateKey = 'ad-autopilot-demo-date';

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
  rememberDemoDate(result);
  return result;
}
