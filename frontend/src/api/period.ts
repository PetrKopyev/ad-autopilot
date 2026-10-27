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
/** «Сегодня» бизнеса, как его считает сервер: в демо — демо-дата, иначе дата по Москве. */
export async function getBusinessToday(signal?: AbortSignal): Promise<string> {
  const demoDate = mockEnabled ? null : await getDemoDate(signal);
  return demoDate ?? new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow' }).format(new Date());
}
export async function getPeriod(choice: PeriodChoice, signal?: AbortSignal): Promise<Period> {
  const demoDate = mockEnabled ? null : await getDemoDate(signal);
  return periodFor(choice, demoDate ? new Date(`${demoDate}T12:00:00`) : new Date());
}
