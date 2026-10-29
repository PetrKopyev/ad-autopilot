import type { ReactNode } from 'react';
import type { AdStatus } from '../api/types';
import { copy } from '../copy';

export type StatusTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

export interface StatusBadgeProps {
  /** Светофор: success — работает, warning — ждём (площадка проверяет), danger — отклонено или нужна помощь,
   *  info — сравниваем варианты, neutral — остановлено или черновик. */
  tone?: StatusTone;
  children: ReactNode;
}

/** Цвет — не единственный носитель смысла: у бейджа всегда есть текст, точка лишь дублирует тон. */
export function StatusBadge({ tone = 'neutral', children }: StatusBadgeProps) {
  return <span className={`status-badge status-badge--${tone}`}><span className="status-badge__dot" aria-hidden="true" />{children}</span>;
}

export const adStatusTone: Record<AdStatus, StatusTone> = {
  draft: 'neutral', moderation: 'warning', active: 'success', paused_auto: 'neutral', paused_owner: 'neutral', rejected: 'danger', archived: 'neutral',
};

/** Статус объявления, группы или кампании словами из copy.status; причина — после двоеточия. */
export function AdStatusBadge({ status, reason }: { status: AdStatus; reason?: string | null }) {
  const label = copy.screens.adStates[status];
  return <StatusBadge tone={adStatusTone[status]}>{reason ? copy.screens.because(label, reason) : label}</StatusBadge>;
}
