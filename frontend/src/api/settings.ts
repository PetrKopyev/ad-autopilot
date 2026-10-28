import { api } from './client';
import { mockEnabled } from './screens';
import { setupGet, setupMutation } from './setup';
import type { Business, Description, Marking, SiteCheck, StopResult } from './setup';

// Описание сервер хранит вместе со служебными данными проверки сайта; форме нужны только поля.
export interface StoredDescription { fields: Description }
// ИНН и телефон сервер целиком не возвращает: inn и phone пустые, а последние цифры — в inn_masked и phone_masked.
export interface StoredMarking extends Omit<Marking, 'confirmed_in_platform'> {
  confirmed_in_platform: Partial<Marking['confirmed_in_platform']>; saved: true; inn_masked: string; phone_masked: string | null;
}
export interface BusinessSettings extends Business {
  description: StoredDescription | null; auto_continue: boolean; marking: StoredMarking | null;
  metrica_counter_id: number | null; lead_goal_ids: number[]; description_approved: boolean; medical_level: SiteCheck['medical_level'];
}
/** Сохранённые реквизиты так, как их показывает сервер: от ИНН и телефона — только последние цифры. */
export function maskMarking(marking: Marking): StoredMarking {
  const phone = marking.phone.replace(/\D/g, '');
  return { ...marking, inn: '', phone: '', saved: true, inn_masked: `•••${marking.inn.slice(-4)}`, phone_masked: phone ? `+7•••${phone.slice(-4)}` : null };
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
export async function deleteMyData(): Promise<StopResult> {
  if (mockEnabled) return (await import('./mocks')).mockDeleteMyData();
  return setupMutation('/api/me', 'DELETE');
}
