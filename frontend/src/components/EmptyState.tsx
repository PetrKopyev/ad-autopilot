import type { ReactNode } from 'react';
import { copy } from '../copy';
import { Icon, type IconName } from './Icon';

export interface EmptyStateProps {
  title?: string;
  description?: string;
  action?: ReactNode;
  icon?: IconName;
}

export function EmptyState({ title = copy.common.noData, description = copy.common.emptyDescription, action, icon = 'chart' }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon"><Icon name={icon} /></span>
      <h2>{title}</h2>
      <p className="muted">{description}</p>
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  );
}
