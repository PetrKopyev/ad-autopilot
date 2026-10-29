import { api, ApiError } from './client';
import { mockEnabled } from './screens';
import { setupGet } from './setup';
import { copy } from '../copy';

export type ChangeKind = 'pause' | 'resume' | 'budget' | 'text' | 'image' | 'launch' | 'revert' | 'experiment_verdict' | 'marking';
export interface HistoryEntry {
  id: number; business_id: number; created_at: string; actor: 'autopilot' | 'owner' | 'system' | 'external';
  kind: ChangeKind; platform: string | null; entity_type: string; entity_id: string;
  /** Название кампании, если запись о кампании: запуск и остановка пишутся по записи на каждую. */
  campaign?: string | null;
  reason_text: string; evidence: Record<string, unknown>; status: 'proposed' | 'pending' | 'applied' | 'failed' | 'reverted';
  reverts_id: number | null; can_revert: boolean; revert_warning: string | null;
}
export interface HistoryPage { items: HistoryEntry[]; total: number; page: number; page_size: number }
export interface HistoryFilter { platform: '' | 'direct' | 'vk'; actor: '' | HistoryEntry['actor'] }

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
  const result = await api<HistoryEntry>(`/api/history/${encodeURIComponent(id)}/revert?confirm=true`, { method: 'POST' });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}

const labels = copy.historyScreen;
// Старт сравнения двух вариантов сервер пишет запуском объявления-варианта; владельцу это не «Запуск рекламы».
function kindTitle(entry: HistoryEntry): string {
  if (entry.kind === 'launch' && 'experiment_key' in entry.evidence) return labels.experimentStart;
  return entry.campaign ? labels.withCampaign(labels.kinds[entry.kind], entry.campaign) : labels.kinds[entry.kind];
}
/** Заголовок записи истории; отмена называет, что отменили, по записи из того же списка, и кампанию. */
export function entryTitle(entry: HistoryEntry, all: HistoryEntry[]): string {
  if (entry.kind !== 'revert') return kindTitle(entry);
  const original = all.find((item) => item.id === entry.reverts_id);
  const campaign = entry.campaign ?? original?.campaign;
  const named = (title: string) => campaign ? labels.withCampaign(title, campaign) : title;
  if (!original) return named(labels.revertedPlain);
  // Отмена остановки одного объявления возвращает только его, а не всю рекламу.
  if (original.kind === 'pause') return named(original.entity_type === 'ad' ? labels.resumedAd : labels.resumedAfterStop);
  if (original.kind === 'launch' && 'experiment_key' in original.evidence) return named(labels.revertedExperiment);
  const text = kindTitle({ ...original, campaign });
  return labels.revertedLine(text.charAt(0).toLowerCase() + text.slice(1));
}
