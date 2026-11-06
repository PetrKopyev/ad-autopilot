import { mockEnabled } from './screens';
import { api, ApiError } from './client';
import { copy } from '../copy';

export interface ForecastSource { what: string; source: string; url: string | null }
/** Прогноз обращений по Директу. Пока он считается или недоступен, чисел нет — только explanation. */
export interface Forecast {
  status: 'ready' | 'pending' | 'unavailable';
  leads_min: number | null; leads_max: number | null; clicks_min: number | null; clicks_max: number | null;
  cpc_rub: number | null; cpc_rub_max: number | null; budget_rub: number | null;
  conversion: { min: number; max: number; source: 'metrica' | 'benchmark'; label: string } | null;
  explanation: string; explanation_by_ai: boolean; sources: ForecastSource[]; computed_at: string | null; demo: boolean;
  vk: { status: 'unavailable'; label: string } | null;
}
export async function getForecast(signal?: AbortSignal): Promise<Forecast> {
  if (mockEnabled) return (await import('./mocks')).mockForecast();
  // Расчёт заказывает отчёт у площадки: изменение, а не чтение.
  const result = await api<Forecast>('/api/plan/forecast', { method: 'POST', signal });
  if (result === undefined) throw new ApiError(copy.errors.invalidResponse);
  return result;
}
