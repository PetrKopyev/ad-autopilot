import type { ReactNode } from 'react';

export interface MetricTileProps {
  label: string;
  /** Число уже отформатировано (formatMoney, toLocaleString) — плитка только задаёт моноширинный шрифт. */
  value: ReactNode;
  /** Мелкая приписка к числу: «₽», «/ 50 000 ₽». */
  unit?: ReactNode;
  /** Подпись внизу простыми словами: откуда число или что оно значит. */
  hint?: ReactNode;
  /** Sparkline или BudgetRing; плитка с кольцом раскладывается в две колонки. */
  chart?: ReactNode;
  wide?: boolean;
  className?: string;
}

export function MetricTile({ label, value, unit, hint, chart, wide, className = '' }: MetricTileProps) {
  return <div className={`metric-tile${wide ? ' metric-tile--wide' : ''} ${className}`}>
    <p className="metric-tile__label">{label}</p>
    <p className="metric-tile__value mono">{value}{unit && <small> {unit}</small>}</p>
    {chart && <div className="metric-tile__chart">{chart}</div>}
    {hint && <p className="metric-tile__hint">{hint}</p>}
  </div>;
}
