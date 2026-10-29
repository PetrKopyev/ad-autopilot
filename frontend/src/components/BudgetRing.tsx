import { copy } from '../copy';
import { formatMoney, Money } from './Money';

export interface BudgetRingProps {
  spentKopecks: number;
  budgetKopecks: number;
  daysRemaining?: number;
}

const radius = 30;
const circumference = 2 * Math.PI * radius;

export function BudgetRing({ spentKopecks, budgetKopecks, daysRemaining }: BudgetRingProps) {
  const label = copy.budget.label(formatMoney(spentKopecks), formatMoney(budgetKopecks));
  if (spentKopecks < 0 || budgetKopecks < 0) throw new RangeError(copy.common.invalidBudget);
  const progress = budgetKopecks === 0 ? 0 : Math.min(1, spentKopecks / budgetKopecks);

  return (
    <div className={`budget-ring${progress >= 0.9 ? ' budget-ring--high' : ''}`} role="group" aria-label={label}>
      <svg viewBox="0 0 72 72" className="budget-ring__graphic" aria-hidden="true">
        <circle cx="36" cy="36" r={radius} className="budget-ring__track" />
        <circle cx="36" cy="36" r={radius} className="budget-ring__fill" transform="rotate(-90 36 36)"
          strokeDasharray={circumference} strokeDashoffset={circumference * (1 - progress)} />
        <text x="36" y="36" dominantBaseline="central" textAnchor="middle" className="budget-ring__percent">{Math.floor(progress * 100)}%</text>
      </svg>
      <div className="budget-ring__text">
        <p className="budget-ring__label">{copy.budget.spent}</p>
        <p className="budget-ring__amount mono"><Money kopecks={spentKopecks} /></p>
        <p className="budget-ring__total">{copy.budget.of} <Money className="mono" kopecks={budgetKopecks} /></p>
        {daysRemaining !== undefined && <p className="budget-ring__days">{copy.budget.remainingDays(daysRemaining)}</p>}
      </div>
    </div>
  );
}
