import { copy } from '../copy';
import { PillSteps } from './Pill';

export interface Step {
  label: string;
  to?: string;
}

/** Шаги мастера таблетками: пройденные — ссылки с галочкой, текущий — тёмный; на телефоне ряд скроллится. */
export function Stepper({ steps, currentStep }: { steps: readonly Step[]; currentStep: number }) {
  return <PillSteps label={copy.setup.stepsLabel} steps={steps} current={currentStep} />;
}
