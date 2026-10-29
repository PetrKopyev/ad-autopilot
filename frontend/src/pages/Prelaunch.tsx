import { Link } from 'react-router';
import { EmptyState } from '../components';
import { copy } from '../copy';
import { getAutopilotStatus } from '../api/screens';
import { useResource } from '../api/useResource';
import type { AutopilotStatus } from '../api/types';

/** До запуска — статус автопилота «draft» (или бизнеса ещё нет): останавливать и показывать пока нечего. */
export const beforeLaunch = (status: AutopilotStatus) => status.status === 'draft' || status.status === 'no_business';

/** Состояние до запуска из того же статуса автопилота, что у меню; пока статус не пришёл — null, не пришёл совсем — false. */
export function usePrelaunch() {
  const { data, error } = useResource(getAutopilotStatus);
  return data && beforeLaunch(data) ? { hasPlan: data.has_plan } : data || error ? false : null;
}

/** Главное действие до запуска: готовый план — открыть его, без плана — настроить рекламу. */
export function PrelaunchLink({ hasPlan, variant = 'primary' }: { hasPlan: boolean; variant?: 'primary' | 'secondary' }) {
  return <Link className={`button button--${variant}`} to={hasPlan ? '/plan' : '/setup'}>
    {hasPlan ? copy.prelaunch.openPlan : copy.prelaunch.setup}</Link>;
}

/** Пустой экран до запуска: «План готов — запустите рекламу» и что здесь появится; без плана — title экрана и путь в мастер. */
export function PrelaunchEmpty({ hasPlan, description, title = copy.prelaunch.noPlan, action = true }: { hasPlan: boolean; description: string; title?: string; action?: boolean }) {
  return <EmptyState icon="pilot" title={hasPlan ? copy.prelaunch.planReady : title}
    description={hasPlan ? description : copy.prelaunch.noPlanHint} action={action && <PrelaunchLink hasPlan={hasPlan} />} />;
}
