import { api } from './client';
import { mockEnabled } from './screens';
import { setupGet } from './setup';

export interface AppNotification {
  id: number; kind: string; created_at: string; title: string; text: string; level: 'info' | 'warning' | 'danger';
}

/** Непрочитанные уведомления, новые сверху. */
export async function getNotifications(signal?: AbortSignal): Promise<AppNotification[]> {
  if (mockEnabled) return (await import('./mocks')).mockNotifications();
  return setupGet('/api/notifications', signal);
}
export async function readNotification(id: number) {
  if (mockEnabled) { (await import('./mocks')).mockReadNotification(id); return; }
  await api(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' });
}
