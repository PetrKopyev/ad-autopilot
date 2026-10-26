import { api, ApiError } from './client';
import { copy } from '../copy';
import type { Ad, AdsResponse, AutopilotStatus, Breakdown, Daily, ExperimentsResponse, HistoryResponse, Period, StatsLevel, Summary } from './types';

export const mockEnabled = (import.meta as ImportMeta & { env: Record<string, string> }).env.VITE_MOCK === '1';

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const result = await api<T>(path, { signal });
  if (result === undefined) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
function query(period: Period): string { return new URLSearchParams({ from: period.from, to: period.to }).toString(); }
export async function getSummary(period: Period, signal?: AbortSignal): Promise<Summary> {
  if (mockEnabled) return (await import('./mocks')).mockSummary(period);
  return get(`/api/stats/summary?${query(period)}`, signal);
}
export async function getBreakdown(period: Period, level: StatsLevel, signal?: AbortSignal): Promise<Breakdown> {
  if (mockEnabled) return (await import('./mocks')).mockBreakdown(period, level);
  return get(`/api/stats/breakdown?${query(period)}&level=${level}`, signal);
}
export async function getDaily(period: Period, signal?: AbortSignal): Promise<Daily> {
  if (mockEnabled) return (await import('./mocks')).mockDaily(period);
  return get(`/api/stats/daily?${query(period)}`, signal);
}
export async function getAutopilotStatus(signal?: AbortSignal): Promise<AutopilotStatus> {
  if (mockEnabled) return (await import('./mocks')).mockStatus();
  return get('/api/autopilot/status', signal);
}
export async function getAds(signal?: AbortSignal): Promise<AdsResponse> {
  if (mockEnabled) return (await import('./mocks')).mockAds();
  return get('/api/ads', signal);
}
export async function setAdStatus(id: number, action: 'pause' | 'resume'): Promise<void> {
  if (mockEnabled) { (await import('./mocks')).mockAdAction(id, action); return; }
  await api<Ad>(`/api/ads/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
}
// A dedicated endpoint is required: pausing only the currently listed ads is not a global stop.
export async function stopAllAds(): Promise<void> {
  if (mockEnabled) { (await import('./mocks')).mockStopAll(); return; }
  await api('/api/autopilot/pause', { method: 'POST' });
}
export async function getExperiments(signal?: AbortSignal): Promise<ExperimentsResponse> {
  if (mockEnabled) return (await import('./mocks')).mockExperiments();
  return get('/api/experiments', signal);
}
export async function getHistory(signal?: AbortSignal): Promise<HistoryResponse> {
  if (mockEnabled) return (await import('./mocks')).mockHistory();
  return get('/api/history?limit=3&page_size=3', signal);
}
