import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { copy } from '../copy';
import { Icon } from './Icon';

export type PillState = 'done' | 'current' | 'upcoming';

/** Шаг-«таблетка»: пройденный — зелёный с галочкой, текущий — тёмный. На телефоне у пройденных остаётся только галочка. */
export function Pill({ state, children }: { state: PillState; children: ReactNode }) {
  return <span className={`pill pill--${state}`}>{state === 'done' && <Icon name="check" />}<span className="pill__label">{children}</span></span>;
}

export interface PillStepsProps {
  label: string;
  steps: readonly { label: string; to?: string }[];
  current: number;
}

/** Ряд шагов; пройденные с адресом — ссылки. Скроллится по горизонтали, если не помещается. */
export function PillSteps({ label, steps, current }: PillStepsProps) {
  return <ol className="pill-steps" aria-label={label}>
    {steps.map((step, index) => {
      const state: PillState = index === current ? 'current' : index < current ? 'done' : 'upcoming';
      const prefix = state === 'current' ? copy.common.currentStep : state === 'done' ? copy.common.completedStep : copy.common.upcomingStep;
      const pill = <Pill state={state}><span className="visually-hidden">{prefix}:</span> {step.label}</Pill>;
      return <li key={step.label} aria-current={state === 'current' ? 'step' : undefined}>
        {step.to && state === 'done' ? <Link to={step.to} className="pill-steps__link">{pill}</Link> : pill}
      </li>;
    })}
  </ol>;
}
