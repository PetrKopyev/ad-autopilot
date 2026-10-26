import type { Period } from './types';

export type PeriodChoice = 'week' | 'month' | '30days';
export function isoDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}
export function getPeriod(choice: PeriodChoice): Period {
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  if (choice === 'month') start.setDate(1);
  else start.setDate(start.getDate() - (choice === 'week' ? 6 : 29));
  return { from: isoDate(start), to: isoDate(today) };
}
export function displayDate(value: string): string {
  return new Date(value.length === 10 ? `${value}T12:00:00` : value).toLocaleDateString('ru-RU');
}
export function displayTime(value: string): string { return new Date(value).toLocaleString('ru-RU'); }
