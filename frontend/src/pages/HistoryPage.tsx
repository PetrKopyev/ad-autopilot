import { useCallback, useState } from 'react';
import { Button, EmptyState, ErrorBanner, PageHeader, PanelCard, PlatformBadge, StatusBadge } from '../components';
import type { StatusTone } from '../components';
import { copy } from '../copy';
import { getHistoryPage, revertChange } from '../api/history';
import type { ChangeKind, HistoryEntry, HistoryFilter } from '../api/history';
import { formatMoney } from '../components/Money';
import { useResource } from '../api/useResource';
import { useBusinessNow } from '../api/useBusinessNow';
import { displayClock, displayDate, justNow } from '../format';
import { platformName } from './StatsShared';
import './History.css';

const pageStep = 20;
const maxPageSize = 100;
const labels = copy.historyScreen;
const statusTone: Record<keyof typeof labels.statuses, StatusTone> = { proposed: 'info', pending: 'warning', failed: 'danger', reverted: 'neutral' };
// Цвет полосы слева повторяет вид действия, а сам вид назван в заголовке записи.
const kindMark: Record<ChangeKind, string> = {
  pause: 'stop', resume: 'go', launch: 'go', budget: 'money', text: 'change', image: 'change', experiment_verdict: 'change', marking: 'plain', revert: 'plain',
};

function author(entry: HistoryEntry): string { return labels.authors[entry.actor]; }
function title(entry: HistoryEntry, all: HistoryEntry[]): string {
  if (entry.kind !== 'revert') return labels.kinds[entry.kind];
  const original = all.find((item) => item.id === entry.reverts_id);
  if (!original) return labels.revertedPlain;
  if (original.kind === 'pause') return labels.resumedAfterStop;
  const text = labels.kinds[original.kind];
  return labels.revertedLine(text.charAt(0).toLowerCase() + text.slice(1));
}
// Отменить остановку — значит продолжить рекламу: так кнопка и подписана.
function revertLabels(entry: HistoryEntry) {
  return entry.kind === 'pause'
    ? { button: labels.resume, icon: 'play' as const, title: labels.resumeTitle, yes: labels.resumeYes, no: labels.resumeNo, busy: labels.resuming }
    : { button: copy.history.undo, icon: 'undo' as const, title: labels.confirmTitle, yes: labels.confirmYes, no: labels.confirmNo, busy: labels.reverting };
}
function figures(entry: HistoryEntry): [string, string][] {
  return (Object.keys(labels.numberLabels) as (keyof typeof labels.numberLabels)[])
    .filter((key) => typeof entry.evidence[key] === 'number')
    .map((key) => {
      const value = entry.evidence[key] as number;
      return [labels.numberLabels[key], key.endsWith('_kop') ? formatMoney(value) : value.toLocaleString('ru-RU')];
    });
}
function dayTitle(day: string, now: number | null): string {
  if (now === null) return day;
  if (day === displayDate(new Date(now).toISOString())) return labels.today(day);
  return day === displayDate(new Date(now - 86_400_000).toISOString()) ? labels.yesterday(day) : day;
}

export function HistoryPage() {
  const [filter, setFilter] = useState<HistoryFilter>({ platform: '', actor: '' });
  const [limit, setLimit] = useState(pageStep);
  const [confirming, setConfirming] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const now = useBusinessNow();
  const history = useResource(useCallback((signal: AbortSignal) => getHistoryPage(filter, limit, signal), [filter, limit]));
  const items = history.data?.items ?? [];
  function change(next: Partial<HistoryFilter>) { setFilter((value) => ({ ...value, ...next })); setLimit(pageStep); setConfirming(undefined); }
  async function revert(id: number) {
    setBusy(true); setError(undefined);
    try { await revertChange(id); setConfirming(undefined); history.reload(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  const days = items.reduce<{ day: string; entries: HistoryEntry[] }[]>((groups, entry) => {
    const day = displayDate(entry.created_at);
    const last = groups[groups.length - 1];
    if (last?.day === day) last.entries.push(entry); else groups.push({ day, entries: [entry] });
    return groups;
  }, []);
  return <div className="page-stack">
    <PageHeader title={copy.history.title} description={labels.intro} />
    <div className="history-toolbar">
      <div className="history-filters">
        <div className="field"><label className="field__label" htmlFor="history-platform">{labels.filterPlatform}</label>
          <select id="history-platform" className="input" value={filter.platform} onChange={(event) => change({ platform: event.target.value as HistoryFilter['platform'] })}>
            <option value="">{labels.all}</option><option value="direct">{platformName('direct')}</option><option value="vk">{platformName('vk')}</option>
          </select></div>
        <div className="field"><label className="field__label" htmlFor="history-actor">{labels.filterAuthor}</label>
          <select id="history-actor" className="input" value={filter.actor} onChange={(event) => change({ actor: event.target.value as HistoryFilter['actor'] })}>
            <option value="">{labels.all}</option><option value="autopilot">{labels.authors.autopilot}</option>
            <option value="owner">{labels.authors.owner}</option><option value="system">{labels.authors.system}</option>
            <option value="external">{labels.authors.external}</option>
          </select></div>
      </div>
      {filter.actor === '' && <p className="history-toolbar__hint">{labels.routineHint}</p>}
    </div>
    {history.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {history.error && <ErrorBanner message={history.error} onRetry={history.reload} />}
    {history.data && !items.length && <div className="panel">{filter.platform || filter.actor
      ? <EmptyState title={labels.emptyFiltered} description={labels.emptyFilteredHint} />
      : <EmptyState title={labels.emptyTitle} description={labels.emptyDescription} />}</div>}
    {days.map(({ day, entries }) => <PanelCard key={day} title={dayTitle(day, now)} titleId={`day-${day}`} flush
      action={<span className="muted">{labels.count(entries.length)}</span>}>
      <ol className="journal">{entries.map((entry) => {
        const numbers = figures(entry);
        const action = revertLabels(entry);
        const reverted = entry.status === 'reverted';
        return <li key={entry.id} className={`journal__entry journal__entry--${reverted ? 'reverted' : entry.status === 'failed' ? 'failed' : kindMark[entry.kind]}`}>
          <time className="journal__time mono" dateTime={entry.created_at}>{justNow(entry.created_at, now) ? labels.justNow : displayClock(entry.created_at)}</time>
          <div className="journal__body">
            <div className="journal__head"><h3>{title(entry, items)}</h3>
              {entry.status in labels.statuses && <StatusBadge tone={statusTone[entry.status as keyof typeof labels.statuses]}>
                {labels.statuses[entry.status as keyof typeof labels.statuses]}</StatusBadge>}</div>
            <p className="journal__meta">{author(entry)}{entry.platform && <> · <PlatformBadge platform={entry.platform} /></>}</p>
            <p className="journal__reason">{(entry.actor === 'autopilot' || entry.actor === 'system') && <span className="journal__label">{labels.why}</span>}{entry.reason_text}</p>
            {numbers.length > 0 && <dl className="journal__numbers" aria-label={labels.numbers}>
              {numbers.map(([name, value]) => <div key={name}><dt>{name}</dt><dd className="mono">{value}</dd></div>)}</dl>}
            {entry.can_revert && confirming !== entry.id && <div className="journal__actions">
              <Button variant="secondary" size="small" icon={action.icon} onClick={() => { setError(undefined); setConfirming(entry.id); }}>{action.button}</Button></div>}
            {confirming === entry.id && <div className="journal__confirm" role="group" aria-label={action.title}>
              <strong>{action.title}</strong>
              {entry.revert_warning && <p>{entry.revert_warning}</p>}
              {error && <ErrorBanner message={error} onDismiss={() => setError(undefined)} />}
              <div className="actions"><Button disabled={busy} onClick={() => void revert(entry.id)}>{busy ? action.busy : action.yes}</Button>
                <Button variant="secondary" disabled={busy} onClick={() => setConfirming(undefined)}>{action.no}</Button></div>
            </div>}
          </div>
        </li>;
      })}</ol>
    </PanelCard>)}
    {history.data && history.data.total > items.length && limit < maxPageSize
      && <div className="actions history-more"><Button variant="secondary" onClick={() => setLimit((value) => Math.min(maxPageSize, value + pageStep))}>{labels.showMore}</Button></div>}
  </div>;
}
