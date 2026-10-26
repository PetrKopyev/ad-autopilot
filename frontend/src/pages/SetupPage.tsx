import { Link, Navigate, useParams } from 'react-router';
import { Card, Stepper } from '../components';
import { copy } from '../copy';
import { NotFoundPage, PlaceholderPage } from './PlaceholderPage';

const steps = copy.setup.steps.map((label, index) => ({ label, to: `/setup/${index + 1}` }));

const descriptions = [
  copy.setup.business.budgetHint,
  copy.setup.accounts.directHint,
  copy.setup.website.blocked,
  copy.setup.draft.question,
  copy.setup.advertiser.explanation,
];

export function SetupPage() {
  const { step } = useParams();
  if (step === undefined) return <Navigate to="/setup/1" replace />;
  if (!/^[1-5]$/.test(step)) return <NotFoundPage />;
  const currentStep = Number(step) - 1;

  return (
    <PlaceholderPage title={copy.setup.title} description={descriptions[currentStep]}>
      <Card>
        <Stepper steps={steps} currentStep={currentStep} />
        <h2 className="step-title">{steps[currentStep].label}</h2>
        <div className="actions">
          {currentStep > 0 && <Link className="button button--secondary" to={steps[currentStep - 1].to}>{copy.common.back}</Link>}
          <Link className="button button--primary" to={steps[currentStep + 1]?.to ?? '/plan'}>{currentStep === 4 ? copy.plan.title : copy.common.next}</Link>
        </div>
      </Card>
    </PlaceholderPage>
  );
}
