import { useEffect, useRef, useState } from 'react';
import { getAuthConfig } from '../api/setup';
import { advanceDemo, getDemoStatus, rememberDemoDate, type DemoStatus } from '../api/demo';
import { useSession } from '../api/session';
import { copy } from '../copy';
import { displayDate, plural } from '../format';
import { Button, Card, ErrorBanner } from './index';

export function DemoPanel({ changed }: { changed: () => void }) {
  const session = useSession();
  const [status, setStatus] = useState<DemoStatus>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retryDays, setRetryDays] = useState<number>();
  const lock = useRef(false);
  const onChanged = useRef(changed);
  onChanged.current = changed;
  const running = status?.advance?.state === 'running';
  useEffect(() => {
    if (!session.data) return;
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then(async (config) => {
      if (!config.demo) return;
      const value = await getDemoStatus(controller.signal);
      if (controller.signal.aborted) return;
      rememberDemoDate(value); setStatus(value); onChanged.current();
    }).catch(() => {});
    return () => controller.abort();
  }, [session.data?.id]);
  useEffect(() => {
    if (!running) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void getDemoStatus(controller.signal).then((value) => {
        if (controller.signal.aborted) return;
        setStatus(value);
        if (value.advance?.state === 'running') return;
        rememberDemoDate(value); onChanged.current();
        if (value.advance?.state === 'error') { setError(value.advance.error); setRetryDays(value.advance.days - value.advance.done); }
      }).catch(() => {});
    }, 1000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [running]);
  async function advance(days: number) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setRetryDays(undefined);
    try { setStatus(await advanceDemo(days)); }
    catch (reason) {
      setError(reason instanceof Error ? reason.message : copy.errors.generic); setRetryDays(days);
      // Перемотка могла уже идти — из другой вкладки или брошенной: показываем её ход, а не прежнее «Перемотано».
      const value = await getDemoStatus().catch(() => undefined);
      if (!value) return;
      setStatus(value);
      if (value.advance?.state === 'running') setError('');
      else if (value.today !== status?.today) { rememberDemoDate(value); onChanged.current(); }
    }
    finally { lock.current = false; setBusy(false); }
  }
  if (!status) return null;
  const progress = status.advance;
  const retry = retryDays && status.total_days + retryDays <= 120 ? retryDays : undefined;
  return <Card className="page-stack">
    <h2>Демо: перемотать время</h2>
    <p className="muted">В демо площадки ненастоящие: сервис ускоряет время, чтобы показать, как работает автопилот. После первой перемотки время в демо идёт только по этим кнопкам.</p>
    <div className="actions demo-advance">{[1, 7, 30].map((days) => <Button key={days} variant="secondary"
      disabled={busy || running || status.total_days + days > 120} onClick={() => void advance(days)}>+{days} {plural(days, 'день', 'дня', 'дней')}</Button>)}</div>
    <p role="status">{running && progress ? `Автопилот обрабатывает прошедшие дни: ${progress.done} из ${progress.days}…`
      : `${progress?.state === 'done' ? 'Готово. ' : ''}Демо-дата: ${displayDate(status.today)}. Перемотано ${status.total_days} из 120 дней.`}</p>
    {error && <ErrorBanner message={error} onRetry={retry ? () => void advance(retry) : undefined} onDismiss={() => setError('')} />}
  </Card>;
}
