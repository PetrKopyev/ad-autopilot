import { useCallback, useState } from 'react';
import { Button, Card, EmptyState, ErrorBanner } from '../components';
import { copy } from '../copy';
import { getHistoryPage, revertChange } from '../api/history';
import type { HistoryEntry, HistoryFilter } from '../api/history';
import { formatMoney } from '../components/Money';
import { useResource } from '../api/useResource';
import { useBusinessNow } from '../api/useBusinessNow';
import { displayClock, displayDate, justNow } from '../format';
import { platformName } from './StatsShared';
import './Setup.css';
import './History.css';

const pageStep = 20;
const maxPageSize = 100;
const labels = copy.historyScreen;

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
    ? { button: labels.resume, title: labels.resumeTitle, yes: labels.resumeYes, no: labels.resumeNo, busy: labels.resuming }
    : { button: copy.history.undo, title: labels.confirmTitle, yes: labels.confirmYes, no: labels.confirmNo, busy: labels.reverting };
}
function figures(entry: HistoryEntry): [string, string][] {
  return (Object.keys(labels.numberLabels) as (keyof typeof labels.numberLabels)[])
    .filter((key) => typeof entry.evidence[key] === 'number')
    .map((key) => {
      const value = entry.evidence[key] as number;
      return [labels.numberLabels[key], key.endsWith('_kop') ? formatMoney(value) : value.toLocaleString('ru-RU')];
    });
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
  return <div className="page-stack launch-flow">
    <h1>{copy.history.title}</h1>
    <p className="muted">{labels.intro}</p>
    <div className="history-filters">
      <label className="field">{labels.filterPlatform}<select value={filter.platform} onChange={(event) => change({ platform: event.target.value as HistoryFilter['platform'] })}>
        <option value="">{labels.all}</option><option value="direct">{platformName('direct')}</option><option value="vk">{platformName('vk')}</option>
      </select></label>
      <label className="field">{labels.filterAuthor}<select value={filter.actor} onChange={(event) => change({ actor: event.target.value as HistoryFilter['actor'] })}>
        <option value="">{labels.all}</option><option value="autopilot">{labels.authors.autopilot}</option>
        <option value="owner">{labels.authors.owner}</option><option value="system">{labels.authors.system}</option>
        <option value="external">{labels.authors.external}</option>
      </select></label>
    </div>
    {filter.actor === '' && <p className="muted">{labels.routineHint}</p>}
    {error && <ErrorBanner message={error} onDismiss={() => setError(undefined)} />}
    {history.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {history.error && <ErrorBanner message={history.error} onRetry={history.reload} />}
    {history.data && !items.length && (filter.platform || filter.actor
      ? <EmptyState title={labels.emptyFiltered} description={labels.emptyFilteredHint} />
      : <EmptyState title={labels.emptyTitle} description={labels.emptyDescription} />)}
    {days.map(({ day, entries }) => <section key={day} className="page-stack" aria-labelledby={`day-${day}`}>
      <h2 id={`day-${day}`}>{day}</h2>
      <ol className="history-day">{entries.map((entry) => {
        const numbers = figures(entry);
        const action = revertLabels(entry);
        return <li key={entry.id}><Card className="history-entry">
          <div className="history-entry__head"><h3>{title(entry, items)}</h3>
            {entry.status in labels.statuses && <span className="history-entry__meta">{labels.statuses[entry.status as keyof typeof labels.statuses]}</span>}</div>
          <p className="history-entry__meta">{justNow(entry.created_at, now) ? labels.justNow : displayClock(entry.created_at)} · {author(entry)}{entry.platform ? ` · ${platformName(entry.platform)}` : ''}</p>
          <p>{entry.reason_text}</p>
          {numbers.length > 0 && <details><summary>{labels.numbers}</summary>
            <dl>{numbers.map(([name, value]) => <div key={name} style={{ display: 'contents' }}><dt>{name}</dt><dd>{value}</dd></div>)}</dl></details>}
          {entry.can_revert && confirming !== entry.id && <div className="actions"><Button variant="secondary" onClick={() => setConfirming(entry.id)}>{action.button}</Button></div>}
          {confirming === entry.id && <div className="history-confirm" role="group" aria-label={action.title}>
            <strong>{action.title}</strong>
            {entry.revert_warning && <p>{entry.revert_warning}</p>}
            <div className="actions"><Button disabled={busy} onClick={() => void revert(entry.id)}>{busy ? action.busy : action.yes}</Button>
              <Button variant="secondary" disabled={busy} onClick={() => setConfirming(undefined)}>{action.no}</Button></div>
          </div>}
        </Card></li>;
      })}</ol>
    </section>)}
    {history.data && history.data.total > items.length && limit < maxPageSize
      && <div className="actions"><Button variant="secondary" onClick={() => setLimit((value) => Math.min(maxPageSize, value + pageStep))}>{labels.showMore}</Button></div>}
  </div>;
}
