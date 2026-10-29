import type { StatusTone } from './StatusBadge';

export interface SparklineProps {
  /** Значения по дням; null — день без данных: линия на нём прерывается, а не падает в ноль. */
  values: readonly (number | null)[];
  tone?: StatusTone | 'accent';
  /** Подпись для чтения с экрана; без неё график декоративный (числа должны быть рядом текстом). */
  label?: string;
  className?: string;
}

const height = 30;

/** Маленький график без осей. Меньше двух точек — ничего не рисуем: тренда из одной точки нет. */
export function Sparkline({ values, tone = 'accent', label, className = '' }: SparklineProps) {
  const known = values.filter((value): value is number => value !== null);
  if (known.length < 2) return null;
  const min = Math.min(...known), max = Math.max(...known);
  const step = 100 / Math.max(1, values.length - 1);
  const y = (value: number) => max === min ? height / 2 : 3 + (height - 6) * (1 - (value - min) / (max - min));
  const segments: string[][] = [[]];
  values.forEach((value, index) => {
    if (value === null) { if (segments[segments.length - 1].length) segments.push([]); return; }
    segments[segments.length - 1].push(`${(index * step).toFixed(2)},${y(value).toFixed(2)}`);
  });
  return <svg className={`sparkline sparkline--${tone} ${className}`} viewBox={`0 0 100 ${height}`} preserveAspectRatio="none"
    {...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true })}>
    {segments.filter((points) => points.length > 1).map((points) => <polyline key={points[0]} points={points.join(' ')} />)}
  </svg>;
}
