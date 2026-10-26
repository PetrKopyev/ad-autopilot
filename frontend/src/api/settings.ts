import { api } from './client';
import { mockEnabled } from './screens';
import { setupGet } from './setup';
import type { Business, Description, Marking } from './setup';

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
  await api('/api/business/budget', { method: 'PUT', body: { month_budget_kop } });
}
export async function saveBusinessSettings(auto_continue: boolean, avg_check_kop: number | null) {
  if (mockEnabled) { (await import('./mocks')).mockSaveBusinessSettings(auto_continue, avg_check_kop); return; }
  await api('/api/business/settings', { method: 'PUT', body: { auto_continue, avg_check_kop } });
}
export async function resumeAutopilot() {
  if (mockEnabled) { (await import('./mocks')).mockResumeAutopilot(); return; }
  await api('/api/autopilot/resume', { method: 'POST' });
}
export async function pauseAutopilot() {
  if (mockEnabled) { (await import('./mocks')).mockPauseAutopilot(); return; }
  await api('/api/autopilot/pause', { method: 'POST' });
}
export async function deleteMyData() {
  if (mockEnabled) { (await import('./mocks')).mockDeleteMyData(); return; }
  await api('/api/me', { method: 'DELETE' });
}
