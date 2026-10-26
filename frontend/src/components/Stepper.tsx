import { Link } from 'react-router';
import { copy } from '../copy';

export interface Step {
  label: string;
  to?: string;
}

export function Stepper({ steps, currentStep }: { steps: readonly Step[]; currentStep: number }) {
  return (
    <nav aria-label={copy.setup.title}>
      <ol className="stepper">
        {steps.map((step, index) => {
          const state = index === currentStep ? copy.common.currentStep
            : index < currentStep ? copy.common.completedStep : copy.common.upcomingStep;
          const content = <><span className="stepper__number" aria-hidden="true">{index + 1}</span><span>{step.label}</span></>;
          return (
            <li key={step.to ?? step.label} className={index === currentStep ? 'stepper__current' : undefined}>
              {step.to ? <Link to={step.to} aria-current={index === currentStep ? 'step' : undefined} aria-label={`${state}: ${step.label}`}>{content}</Link>
                : <span aria-current={index === currentStep ? 'step' : undefined} aria-label={`${state}: ${step.label}`}>{content}</span>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
