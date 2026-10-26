import { ApiError } from './client';
import { mockEnabled } from './screens';
import { setupGet, setupRequest } from './setup';
import type { Business, Description, Marking } from './setup';
import { copy } from '../copy';

// Proposed contract: the backend has no settings endpoints yet.
export interface BusinessSettings extends Business {
  description: Description | null; auto_continue: boolean; marking: Marking | null;
}
export async function getBusiness(signal?: AbortSignal): Promise<BusinessSettings> {
  if (mockEnabled) return (await import('./mocks')).mockBusinessSettings();
  return setupGet('/api/business', signal);
}
export async function saveBudget(month_budget_kop: number) {
  if (mockEnabled) { (await import('./mocks')).mockSaveBudget(month_budget_kop); return; }
  await setupRequest('/api/business/budget', { method: 'PUT', body: { month_budget_kop } });
}
export async function saveBusinessSettings(auto_continue: boolean, avg_check_kop: number | null) {
  if (mockEnabled) { (await import('./mocks')).mockSaveBusinessSettings(auto_continue, avg_check_kop); return; }
  await setupRequest('/api/business/settings', { method: 'PUT', body: { auto_continue, avg_check_kop } });
}
export async function resumeAutopilot() {
  if (mockEnabled) { (await import('./mocks')).mockResumeAutopilot(); return; }
  await setupRequest('/api/autopilot/resume', { method: 'POST' });
}
export async function pauseAutopilot() {
  if (mockEnabled) { (await import('./mocks')).mockPauseAutopilot(); return; }
  await setupRequest('/api/autopilot/pause', { method: 'POST' });
}
export async function deleteMyData() {
  if (mockEnabled) { (await import('./mocks')).mockDeleteMyData(); return; }
  await setupRequest('/api/me', { method: 'DELETE' });
}
const rubleFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
// Whole rubles are shown without ",00".
export function formatRubles(kopecks: number): string {
  if (!Number.isSafeInteger(kopecks)) throw new ApiError(copy.common.invalidMoney);
  return `${rubleFormat.format(kopecks / 100)} ₽`;
}
