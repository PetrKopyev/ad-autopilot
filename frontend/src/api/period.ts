import type { Period } from './types';
import { getDemoDate } from './demo';
import { mockEnabled } from './screens';

export type PeriodChoice = 'week' | 'month' | '30days';
export function isoDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
export function periodFor(choice: PeriodChoice, today = new Date()): Period {
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  if (choice === 'month') start.setDate(1);
  else start.setDate(start.getDate() - (choice === 'week' ? 6 : 29));
  return { from: isoDate(start), to: isoDate(today) };
}
/** Последние days дней по today включительно. */
export function lastDays(days: number, today: string): Period {
  const start = new Date(`${today}T12:00:00`);
  start.setDate(start.getDate() - days + 1);
  return { from: isoDate(start), to: today };
}
/** «Сегодня» бизнеса, как его считает сервер: в демо — демо-дата, иначе дата по Москве. */
export async function getBusinessToday(signal?: AbortSignal): Promise<string> {
  const demoDate = mockEnabled ? null : await getDemoDate(signal);
  return demoDate ?? new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow' }).format(new Date());
}
/** Средний расход за день: бюджет месяца на дни текущего месяца по Москве. Дневной лимит кампании ниже среднего —
 * площадки перерасходуют его, поэтому владельцу показываем среднее. От 100 ₽ — целые рубли вниз: среднее не больше бюджета. */
export function dailyAverage(monthKop: number, now = new Date()): number {
  const [year, month] = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow' }).format(now).split('-').map(Number);
  return perDay(monthKop, new Date(year, month, 0).getDate());
}
/** Сумма за days дней в среднем на день — как dailyAverage: план и главная показывают одно и то же число. */
export function perDay(kopecks: number, days: number): number {
  const average = Math.floor(kopecks / days);
  return average >= 10_000 ? average - average % 100 : average;
}
export async function getPeriod(choice: PeriodChoice, signal?: AbortSignal): Promise<Period> {
  return periodFor(choice, new Date(`${await getBusinessToday(signal)}T12:00:00`));
}
