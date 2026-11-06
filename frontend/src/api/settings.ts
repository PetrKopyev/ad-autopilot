import { ApiError, api } from './client';
import { copy } from '../copy';
import { mockEnabled } from './screens';
import { setupGet, setupMutation } from './setup';
import type { Business, Description, Marking, SiteCheck, StopResult } from './setup';
import type { MarkingTransmission } from './types';

// Описание сервер хранит вместе со служебными данными проверки сайта; форме нужны только поля.
export interface StoredDescription { fields: Description }
// ИНН и телефон сервер целиком не возвращает: inn и phone пустые, а последние цифры — в inn_masked и phone_masked.
export interface StoredMarking extends Omit<Marking, 'confirmed_in_platform'> {
  confirmed_in_platform: Partial<Marking['confirmed_in_platform']>; saved: true; inn_masked: string; phone_masked: string | null;
  /** Статус передачи данных рекламодателя по площадкам; null — передачи не было или кабинет не подключён. */
  transmission: MarkingTransmission;
}
export interface BusinessSettings extends Business {
  description: StoredDescription | null; auto_continue: boolean; marking: StoredMarking | null;
  metrica_counter_id: number | null; lead_goal_ids: number[]; description_approved: boolean; medical_level: SiteCheck['medical_level'];
  /** Цель пикселя кабинета VK: номер пикселя и название цели; null — не задана. */
  vk_priced_goal_source_id?: number | null; vk_priced_goal_name?: string | null;
}
/** Сохранённые реквизиты так, как их показывает сервер: от ИНН и телефона — только последние цифры. */
export function maskMarking(marking: Marking, transmission?: MarkingTransmission): StoredMarking {
  const phone = marking.phone.replace(/\D/g, '');
  return { ...marking, inn: '', phone: '', saved: true, inn_masked: `•••${marking.inn.slice(-4)}`, phone_masked: phone ? `+7•••${phone.slice(-4)}` : null,
    transmission: transmission ?? { direct: null, vk: null } };
}
export async function getBusiness(signal?: AbortSignal): Promise<BusinessSettings> {
  if (mockEnabled) return (await import('./mocks')).mockBusinessSettings();
  return setupGet('/api/business', signal);
}
export async function saveBudget(month_budget_kop: number): Promise<string | null> {
  if (mockEnabled) { (await import('./mocks')).mockSaveBudget(month_budget_kop); return null; }
  return (await api<{ warning: string | null }>('/api/business/budget', { method: 'PUT', body: { month_budget_kop } }))?.warning ?? null;
}
export async function saveBusinessSettings(auto_continue: boolean, avg_check_kop: number | null) {
  if (mockEnabled) { (await import('./mocks')).mockSaveBusinessSettings(auto_continue, avg_check_kop); return; }
  await api('/api/business/settings', { method: 'PUT', body: { auto_continue, avg_check_kop } });
}
/** Цель пикселя VK; source_id = 0 убирает её. Сервер сохраняет настройки целиком: «продолжать» и средний чек
 * берём сохранённые, а не из несохранённой формы бюджета. */
export async function saveVkGoal(source_id: number, name: string): Promise<BusinessSettings> {
  if (mockEnabled) return (await import('./mocks')).mockSaveVkGoal(source_id, name);
  const { auto_continue, avg_check_kop } = await getBusiness();
  const result = await api<BusinessSettings>('/api/business/settings', { method: 'PUT',
    body: { auto_continue, avg_check_kop, vk_priced_goal_source_id: source_id, vk_priced_goal_name: name } });
  if (result === undefined) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function resumeAutopilot(): Promise<string | null> {
  if (mockEnabled) { (await import('./mocks')).mockResumeAutopilot(); return null; }
  return (await api<{ warning: string | null }>('/api/autopilot/resume', { method: 'POST' }))?.warning ?? null;
}
export async function pauseAutopilot() {
  if (mockEnabled) { (await import('./mocks')).mockPauseAutopilot(); return; }
  await api('/api/autopilot/pause', { method: 'POST' });
}
export async function deleteMyData(): Promise<StopResult> {
  if (mockEnabled) return (await import('./mocks')).mockDeleteMyData();
  return setupMutation('/api/me', 'DELETE');
}
