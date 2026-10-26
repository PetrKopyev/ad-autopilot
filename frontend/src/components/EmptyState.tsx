import type { ReactNode } from 'react';
import { copy } from '../copy';

export interface EmptyStateProps {
  title?: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({ title = copy.common.noData, description = copy.common.emptyDescription, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon" aria-hidden="true">○</span>
      <h2>{title}</h2>
      <p className="muted">{description}</p>
      {action && <div className="empty-state__action">{action}</div>}
    </div>
  );
}
