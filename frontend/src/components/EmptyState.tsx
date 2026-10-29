import type { ReactNode } from 'react';
import { copy } from '../copy';
import { Icon, type IconName } from './Icon';

export interface EmptyStateProps {
  title?: string;
  description?: string;
  action?: ReactNode;
  icon?: IconName;
  /** Пустое состояние вместо всей страницы (404) — заголовок страницы. */
  level?: 1 | 2;
}

export function EmptyState({ title = copy.common.noData, description = copy.common.emptyDescription, action, icon = 'chart', level = 2 }: EmptyStateProps) {
  const Heading = level === 1 ? 'h1' : 'h2';
  return (
    <div className="empty-state">
      <span className="empty-state__icon"><Icon name={icon} /></span>
      <Heading className="empty-state__title">{title}</Heading>
      <p className="muted">{description}</p>
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  );
}
