import { api, ApiError } from './client';
import { mockEnabled } from './screens';
import { copy } from '../copy';
import { demoDateKey } from './demo';

export interface Account {
  id: number; business_id: number; platform: 'direct' | 'vk'; external_login: string;
  status: 'ok' | 'needs_reauth' | 'revoked'; scopes: string[];
}
/** has_business — мастер создал бизнес: без него разделы с данными ведут в мастер. */
export interface Me { id: number; login: string; email: string | null; has_business: boolean; accounts: Account[] }
export interface BusinessInput { site_url: string; city: string; month_budget_kop: number; avg_check_kop: number | null }
export interface Business extends BusinessInput { id: number }
export type DescriptionKey = 'services' | 'prices' | 'address' | 'difference';
export interface DescriptionField { value: string; quote: string | null; verified: boolean; question: string }
export type Description = Record<DescriptionKey, DescriptionField>;
export interface SiteCheck {
  status: 'reading' | 'ready' | 'blocked'; pages_read: number; services_found: number;
  counter_id: number | null; description: Description; medical_level: 'none' | 'maybe' | 'required';
}
/** Перед отзывом доступа сервер останавливает рекламу; stopped=false — площадка остановку не подтвердила. */
export interface StopResult { stopped: boolean; message: string }
/** Площадка, которую бюджет месяца не пропускает: from_kop — бюджет, с которого она подключится. */
export interface PlatformOff { platform: 'vk'; from_kop: number; min_daily_kop: number }
/** Подсказка к бюджету: площадки и дневные лимиты считает сервер так же, как для плана и запуска. */
export interface BudgetHint { minimum_kop: number; low_kop: number; daily_kop: { direct?: number; vk?: number }; off: PlatformOff[] }
export interface Counter { id: number; name: string }
export interface Goal { id: number; name: string }
export interface Marking {
  subject_type: 'ip' | 'ooo' | 'selfemployed' | 'person'; inn: string; name: string; phone: string;
  confirmed_in_platform: { direct: boolean; vk: boolean };
}
export async function setupGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const result = await api<T>(path, { signal });
  if (result === undefined) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function setupMutation<T>(path: string, method: string): Promise<T> {
  const result = await api<T>(path, { method });
  if (result === undefined) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function getMe(signal?: AbortSignal): Promise<Me> {
  if (mockEnabled) return (await import('./mocks')).mockSetupMe();
  return setupGet('/api/me', signal);
}
export async function getAuthConfig(signal?: AbortSignal): Promise<{ demo: boolean }> {
  return setupGet('/api/auth/config', signal);
}
export async function demoLogin() {
  await api('/api/auth/demo', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' } });
  try { sessionStorage.removeItem('ad-autopilot-setup-draft'); sessionStorage.removeItem(demoDateKey); } catch { /* Storage may be unavailable. */ }
}
export async function logout() {
  if (mockEnabled) (await import('./mocks')).mockSetupLogout();
  else await api('/api/auth/logout', { method: 'POST' });
  try { sessionStorage.removeItem('ad-autopilot-setup-draft'); } catch { /* Storage may be unavailable. */ }
  try { sessionStorage.removeItem(demoDateKey); } catch { /* Storage may be unavailable. */ }
}
export async function disconnectAccount(id: number): Promise<StopResult> {
  if (mockEnabled) return (await import('./mocks')).mockSetupDisconnect(id);
  return setupMutation(`/api/accounts/${encodeURIComponent(id)}`, 'DELETE');
}
export async function connectAccount(platform: 'login' | 'direct' | 'vk', businessId?: number) {
  if (mockEnabled) { (await import('./mocks')).mockSetupConnect(platform); return; }
  const params = new URLSearchParams();
  if (platform !== 'vk') params.set('purpose', platform);
  if (businessId) params.set('business_id', String(businessId));
  window.location.assign(`/api/auth/${platform === 'vk' ? 'vk' : 'yandex'}/start?${params}`);
}
export async function connectVkKey(client_id: string, client_secret: string, businessId: number) {
  if (mockEnabled) { (await import('./mocks')).mockSetupConnect('vk'); return; }
  await api('/api/accounts/vk/key', { method: 'POST', body: { client_id, client_secret, business_id: businessId } });
}
export async function createBusiness(body: BusinessInput): Promise<Business> {
  if (mockEnabled) return (await import('./mocks')).mockSetupBusiness(body);
  const result = await api<Business>('/api/business', { method: 'POST', body });
  if (!result) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
export async function getBudgetHint(month_budget_kop: number, vk: boolean, signal?: AbortSignal): Promise<BudgetHint> {
  if (mockEnabled) return (await import('./mocks')).mockBudgetHint(month_budget_kop, vk);
  return setupGet(`/api/business/budget-hint?month_budget_kop=${month_budget_kop}&vk=${vk}`, signal);
}
export async function checkSite(signal?: AbortSignal, refresh = false): Promise<SiteCheck> {
  if (mockEnabled) return (await import('./mocks')).mockSetupSite();
  return setupGet(`/api/business/site-check${refresh ? '?refresh=1' : ''}`, signal);
}
export async function saveDescription(description: Description) {
  if (mockEnabled) { (await import('./mocks')).mockSetupDescription(description); return; }
  await api('/api/business/description', { method: 'PUT', body: { description, approved: true } });
}
export async function getCounters(signal?: AbortSignal): Promise<{ items: Counter[] }> {
  if (mockEnabled) return (await import('./mocks')).mockSetupCounters();
  return setupGet('/api/metrica/counters', signal);
}
export async function getGoals(counter: number, signal?: AbortSignal): Promise<{ items: Goal[] }> {
  if (mockEnabled) return (await import('./mocks')).mockSetupGoals(counter);
  return setupGet(`/api/metrica/goals?counter=${encodeURIComponent(counter)}`, signal);
}
export async function saveMetrica(counter_id: number, lead_goal_ids: number[]) {
  if (mockEnabled) { (await import('./mocks')).mockSetupMetrica(counter_id, lead_goal_ids); return; }
  await api('/api/business/metrica', { method: 'PUT', body: { counter_id, lead_goal_ids } });
}
export async function saveMarking(body: Marking) {
  if (mockEnabled) { (await import('./mocks')).mockSetupMarking(body); return; }
  await api('/api/business/marking', { method: 'PUT', body });
}
export function validInn(value: string): boolean {
  if (!/^(?:\d{10}|\d{12})$/.test(value) || /^0+$/.test(value)) return false;
  const digits = [...value].map(Number);
  const checksum = (weights: number[]) => weights.reduce((sum, weight, index) => sum + weight * digits[index], 0) % 11 % 10;
  return digits.length === 10
    ? checksum([2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[9]
    : checksum([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[10]
      && checksum([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === digits[11];
}
// Parse decimal rubles without floating point rounding.
export function rublesToKopecks(value: string): number | null {
  if (!/^\d+(?:[.,]\d{1,2})?$/.test(value)) return null;
  const [whole, fraction = ''] = value.replace(',', '.').split('.');
  const result = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
  return Number.isSafeInteger(result) && result > 0 ? result : null;
}
