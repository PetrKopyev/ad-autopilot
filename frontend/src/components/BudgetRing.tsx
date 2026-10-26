import { copy } from '../copy';
import { formatMoney, Money } from './Money';

export interface BudgetRingProps {
  spentKopecks: number;
  budgetKopecks: number;
  daysRemaining?: number;
}

export function BudgetRing({ spentKopecks, budgetKopecks, daysRemaining }: BudgetRingProps) {
  const label = copy.budget.label(formatMoney(spentKopecks), formatMoney(budgetKopecks));
  if (spentKopecks < 0 || budgetKopecks < 0) throw new RangeError(copy.common.invalidBudget);
  const progress = budgetKopecks === 0 ? 0 : Math.min(1, spentKopecks / budgetKopecks);
  const circumference = 2 * Math.PI * 48;

  return (
    <div className="budget-ring" role="group" aria-label={label}>
      <svg viewBox="0 0 112 112" className="budget-ring__graphic" aria-hidden="true">
        <circle cx="56" cy="56" r="48" className="budget-ring__track" />
        <circle cx="56" cy="56" r="48" className="budget-ring__fill"
          strokeDasharray={circumference} strokeDashoffset={circumference * (1 - progress)} />
      </svg>
      <div>
        <p className="muted">{copy.budget.spent}</p>
        <p className="budget-ring__amount"><Money kopecks={spentKopecks} /></p>
        <p>{copy.budget.of} <Money kopecks={budgetKopecks} /></p>
        {daysRemaining !== undefined && <p className="muted">{copy.budget.remainingDays(daysRemaining)}</p>}
      </div>
    </div>
  );
}
