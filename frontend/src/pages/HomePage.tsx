import { useCallback, useRef, useState } from 'react';
import { Link } from 'react-router';
import { BudgetRing, Button, Card, EmptyState, ErrorBanner, StatusBadge } from '../components';
import { copy } from '../copy';
import { getAutopilotStatus, getHistory, getSummary, stopAllAds } from '../api/screens';
import { getPeriod, type PeriodChoice } from '../api/period';
import { useResource } from '../api/useResource';
import { useBusinessNow } from '../api/useBusinessNow';
import { getNotifications, readNotification, type AppNotification } from '../api/notifications';
import { displayDate, displayDateTime, justNow } from '../format';
import { ClientsSummary, DemoNotice, PeriodPicker, Sources, SummaryFigures } from './StatsShared';
import { DemoPanel } from '../components/DemoPanel';
import { resumeAutopilot } from '../api/settings';
import './Home.css';

function Notifications({ items, changed }: { items: AppNotification[]; changed: () => void }) {
  const [busy, setBusy] = useState<number>();
  const [error, setError] = useState('');
  async function read(id: number) {
    setBusy(id); setError('');
    try { await readNotification(id); changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(undefined); }
  }
  return <Card className="page-stack" aria-labelledby="notifications-title"><h2 id="notifications-title">{copy.home.notifications}</h2>
    {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    {items.map((item) => <article key={item.id} className={`home-notice home-notice--${item.level}`}>
      <h3>{item.title}</h3>
      <time className="muted" dateTime={item.created_at}>{displayDateTime(item.created_at)}</time>
      <p>{item.text}</p>
      <div className="actions"><Button variant="secondary" disabled={busy !== undefined} onClick={() => void read(item.id)}>{copy.home.understood}</Button></div>
    </article>)}
  </Card>;
}

export function HomePage() {
  const [choice, setChoice] = useState<PeriodChoice>('month');
  const summary = useResource(useCallback(async (signal: AbortSignal) => getSummary(await getPeriod(choice, signal), signal), [choice]));
  const autopilot = useResource(getAutopilotStatus);
  const history = useResource(getHistory);
  const notifications = useResource(getNotifications);
  const dialog = useRef<HTMLDialogElement>(null);
  const stopLock = useRef(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string>();
  const [stopped, setStopped] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState('');
  const now = useBusinessNow();
  const status = autopilot.data;
  // В демо точного времени бизнеса нет: показываем время проверки, а не «N минут назад», и не судим о давности.
  const minutes = status?.last_check_at && now !== null ? Math.max(0, Math.floor((now - Date.parse(status.last_check_at)) / 60000)) : null;
  const stale = status?.status === 'running' && now !== null && (minutes === null || minutes > 15);
  const working = !status?.last_check_at ? copy.screens.heartbeatMissing
    : minutes === null ? copy.home.checkedAt(displayDateTime(status.last_check_at)) : copy.home.working(minutes);
  const statusText = status?.status === 'running' ? working
    : status?.status === 'budget_stop' ? (status.auto_continue ? copy.home.exhausted : copy.screens.exhaustedPaused)
      : status?.status === 'draft' ? copy.screens.draft : copy.home.paused;
  async function stop() {
    if (stopLock.current) return;
    stopLock.current = true; setStopping(true); setStopError(undefined);
    try { await stopAllAds(); setStopped(true); dialog.current?.close(); autopilot.reload(); history.reload(); }
    catch (error) { setStopError(error instanceof Error ? error.message : copy.errors.generic); }
    finally { stopLock.current = false; setStopping(false); }
  }
  return <div className="page-stack">
    <div className="home-heading"><div className="page-heading"><h1>{copy.home.title}</h1><p className="muted">{copy.screens.intro}</p></div>
      <Button variant="danger" onClick={() => { setStopError(undefined); dialog.current?.showModal(); }} disabled={stopping || stopped || status?.status === 'paused'}>{copy.home.stopAll}</Button>
    </div>
    <DemoNotice />
    <DemoPanel changed={() => { summary.reload(); autopilot.reload(); history.reload(); notifications.reload(); }} />
    {/* Уведомления — дополнение к главной: если их нет или они не загрузились, блока нет. */}
    {notifications.data && notifications.data.length > 0 && <Notifications items={notifications.data} changed={notifications.reload} />}
    {stopped && <p role="status">{copy.screens.stopped}</p>}
    <Card className="page-stack">
      {autopilot.loading && <p role="status">{copy.screens.loading}</p>}
      {autopilot.error && <ErrorBanner message={autopilot.error} onRetry={autopilot.reload} />}
      {status && <StatusBadge tone={stale ? 'danger' : status.status === 'running' ? 'success' : 'warning'}>{statusText}</StatusBadge>}
      {status?.status === 'paused' && <Button disabled={resuming} onClick={() => {
        setResuming(true); setResumeError('');
        void resumeAutopilot().then(() => { setStopped(false); autopilot.reload(); history.reload(); })
          .catch((reason: unknown) => setResumeError(reason instanceof Error ? reason.message : copy.errors.generic))
          .finally(() => setResuming(false));
      }}>{copy.settings.resume}</Button>}
      {resumeError && <ErrorBanner message={resumeError} onDismiss={() => setResumeError('')} />}
      {stale && <ErrorBanner message={copy.screens.heartbeatStale} />}
      {summary.data && <BudgetRing spentKopecks={summary.data.budget.spent_kop} budgetKopecks={summary.data.budget.budget_kop} daysRemaining={summary.data.budget.days_remaining} />}
    </Card>
    <PeriodPicker value={choice} onChange={setChoice} />
    {summary.loading && <p role="status">{copy.screens.loading}</p>}
    {summary.error && <ErrorBanner message={summary.error} onRetry={summary.reload} />}
    {summary.data && <>
      <p className="muted">{copy.screens.dates(displayDate(summary.data.from), displayDate(summary.data.to))}</p>
      <SummaryFigures summary={summary.data} />
      {summary.data.sources.length === 0 && <Card><EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} /></Card>}
      <ClientsSummary summary={summary.data} />
      <Sources sources={summary.data.sources} />
    </>}
    <div className="home-columns">
      <Card className="page-stack"><h2>{copy.home.needsAttention}</h2>
        {autopilot.loading && <p role="status">{copy.screens.loading}</p>}
        {status && (status.needs_attention.length ? status.needs_attention.map((item) => <div className="home-list-item" key={item.id}>
          <h3>{item.title}</h3><p>{item.description}</p><Link to={`/${item.destination}`}>{copy.screens.open}</Link>
        </div>) : <EmptyState title={copy.screens.noAttention} description={copy.screens.noAttentionHint} />)}
      </Card>
      <Card className="page-stack"><h2>{copy.home.recent}</h2>
        {history.loading && <p role="status">{copy.screens.loading}</p>}
        {history.error && <ErrorBanner message={history.error} onRetry={history.reload} />}
        {history.data && (history.data.items.length ? history.data.items.slice(0, 3).map((item) => <div className="home-list-item" key={item.id}>
          <time className="muted" dateTime={item.created_at}>{justNow(item.created_at, now) ? copy.historyScreen.justNow : displayDateTime(item.created_at)}</time>
          <p>{item.reason_text}</p><StatusBadge>{copy.screens.historyStates[item.status as keyof typeof copy.screens.historyStates] ?? copy.screens.pending}</StatusBadge>
        </div>) : <EmptyState title={copy.screens.noHistory} description={copy.screens.historyHint} />)}
        <Link to="/history">{copy.screens.allHistory}</Link>
      </Card>
    </div>
    <dialog ref={dialog} className="home-confirm" aria-labelledby="stop-title" aria-describedby="stop-description" onCancel={(event) => { if (stopping) event.preventDefault(); }}>
      <div className="page-stack"><h2 id="stop-title">{copy.screens.stopTitle}</h2><p id="stop-description">{copy.screens.stopDescription}</p>
        {stopError && <ErrorBanner message={stopError} />}
        <div className="actions"><Button autoFocus variant="secondary" disabled={stopping} onClick={() => dialog.current?.close()}>{copy.screens.cancel}</Button>
          <Button variant="danger" disabled={stopping} onClick={() => void stop()}>{stopping ? copy.screens.stopping : copy.screens.confirmStop}</Button></div>
      </div>
    </dialog>
  </div>;
}
