import type { StatusTone } from './StatusBadge';

export interface SparklineProps {
  /** Значения по дням; null — день без данных: линия на нём прерывается, а не падает в ноль. */
  values: readonly (number | null)[];
  tone?: StatusTone | 'accent';
  /** Подпись для чтения с экрана; без неё график декоративный (числа должны быть рядом текстом). */
  label?: string;
  className?: string;
  /** Дни, цифры которых ещё уточняются: отрезки к ним рисуются пунктиром. */
  dashed?: readonly boolean[];
  /** null — не пропуск данных, а день, где величина не определена (цена без обращений): соседние точки соединяем. */
  connect?: boolean;
}

const height = 30;

/** Маленький график без осей. Меньше двух точек — ничего не рисуем: тренда из одной точки нет. */
export function Sparkline({ values, tone = 'accent', label, className = '', dashed, connect }: SparklineProps) {
  const known = values.filter((value): value is number => value !== null);
  if (known.length < 2) return null;
  const min = Math.min(...known), max = Math.max(...known);
  const step = 100 / Math.max(1, values.length - 1);
  const point = (index: number, value: number) =>
    `${(index * step).toFixed(2)},${(max === min ? height / 2 : 3 + (height - 6) * (1 - (value - min) / (max - min))).toFixed(2)}`;
  const lines: { points: string[]; dashed: boolean; end: number }[] = [];
  let previous: number | undefined;
  values.forEach((value, index) => {
    if (value === null) { if (!connect) previous = undefined; return; }
    if (previous !== undefined) {
      const isDashed = !!(dashed?.[previous] || dashed?.[index]);
      const last = lines[lines.length - 1];
      if (last && last.end === previous && last.dashed === isDashed) { last.points.push(point(index, value)); last.end = index; }
      else lines.push({ points: [point(previous, values[previous]!), point(index, value)], dashed: isDashed, end: index });
    }
    previous = index;
  });
  return <svg className={`sparkline sparkline--${tone} ${className}`} viewBox={`0 0 100 ${height}`} preserveAspectRatio="none"
    {...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true })}>
    {lines.map((line) => <polyline key={line.points[0]} className={line.dashed ? 'sparkline__dashed' : undefined} points={line.points.join(' ')} />)}
  </svg>;
}
