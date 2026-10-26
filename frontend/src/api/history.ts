import { ApiError } from './client';
import { mockEnabled } from './screens';
import { setupGet, setupRequest } from './setup';
import { copy } from '../copy';

export type ChangeKind = 'pause' | 'resume' | 'budget' | 'text' | 'image' | 'launch' | 'revert' | 'experiment_verdict' | 'marking';
export interface HistoryEntry {
  id: number; business_id: number; created_at: string; actor: 'autopilot' | 'owner' | 'system' | 'external';
  kind: ChangeKind; platform: string | null; entity_type: string; entity_id: string;
  reason_text: string; evidence: Record<string, unknown>; status: 'proposed' | 'pending' | 'applied' | 'failed' | 'reverted';
  reverts_id: number | null; can_revert: boolean; revert_warning: string | null;
}
export interface HistoryPage { items: HistoryEntry[]; total: number; page: number; page_size: number }
export interface HistoryFilter { platform: '' | 'direct' | 'vk'; actor: '' | 'autopilot' | 'owner' | 'external' }

export async function getHistoryPage(filter: HistoryFilter, pageSize: number, signal?: AbortSignal): Promise<HistoryPage> {
  if (mockEnabled) return (await import('./mocks')).mockHistoryPage(filter, pageSize);
  const params = new URLSearchParams({ page: '1', page_size: String(pageSize) });
  if (filter.platform) params.set('platform', filter.platform);
  if (filter.actor) params.set('actor', filter.actor);
  return setupGet(`/api/history?${params}`, signal);
}
// The owner has already confirmed on the page, so the server's own confirmation step is passed through.
export async function revertChange(id: number): Promise<HistoryEntry> {
  if (mockEnabled) return (await import('./mocks')).mockRevert(id);
  const result = await setupRequest<HistoryEntry>(`/api/history/${encodeURIComponent(id)}/revert?confirm=true`, { method: 'POST' });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
