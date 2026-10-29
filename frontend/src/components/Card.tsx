import type { HTMLAttributes, ReactNode } from 'react';

export function Card({ className = '', ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div {...props} className={`card ${className}`} />;
}

export interface PanelCardProps {
  /** Заголовок панели (h2); id нужен, чтобы секция была названа заголовком. */
  title: ReactNode;
  titleId: string;
  /** Справа в шапке: ссылка «История →», бейдж, счётчик. */
  action?: ReactNode;
  /** Содержимое без внутренних отступов — для таблиц и списков на всю ширину. */
  flush?: boolean;
  className?: string;
  children: ReactNode;
}

export function PanelCard({ title, titleId, action, flush, className = '', children }: PanelCardProps) {
  return <section className={`panel ${className}`} aria-labelledby={titleId}>
    <div className="panel__head"><h2 id={titleId}>{title}</h2>{action && <div className="panel__action">{action}</div>}</div>
    <div className={flush ? 'panel__body panel__body--flush' : 'panel__body'}>{children}</div>
  </section>;
}
