import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { BudgetRing, Button, Card, EmptyState, ErrorBanner, StatusBadge } from '../components';
import { copy } from '../copy';
import { getAutopilotStatus, getHistory, getSummary, stopAllAds } from '../api/screens';
import { displayDate, displayTime, getPeriod, type PeriodChoice } from '../api/period';
import { useResource } from '../api/useResource';
import { ClientsSummary, DemoNotice, PeriodPicker, Sources, SummaryFigures } from './StatsShared';
import { DemoPanel } from '../components/DemoPanel';
import { resumeAutopilot } from '../api/settings';
import './Home.css';

export function HomePage() {
  const [choice, setChoice] = useState<PeriodChoice>('month');
  const summary = useResource(useCallback((signal: AbortSignal) => getSummary(getPeriod(choice), signal), [choice]));
  const autopilot = useResource(getAutopilotStatus);
  const history = useResource(getHistory);
  const dialog = useRef<HTMLDialogElement>(null);
  const stopLock = useRef(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string>();
  const [stopped, setStopped] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState('');
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60000);
    return () => window.clearInterval(timer);
  }, []);
  const status = autopilot.data;
  const minutes = status?.last_check_at ? Math.max(0, Math.floor((now - Date.parse(status.last_check_at)) / 60000)) : null;
  const stale = status?.status === 'running' && (minutes === null || minutes > 15);
  const statusText = status?.status === 'running' ? (minutes === null ? copy.screens.heartbeatMissing : copy.home.working(minutes))
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
    <DemoPanel changed={() => { summary.reload(); autopilot.reload(); history.reload(); }} />
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
          <time className="muted" dateTime={item.created_at}>{displayTime(item.created_at)}</time>
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
