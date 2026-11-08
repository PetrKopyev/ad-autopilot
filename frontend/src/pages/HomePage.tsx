import { useCallback, useRef, useState } from 'react';
import { Link } from 'react-router';
import { AdStatusBadge, BudgetRing, Button, ConfirmDialog, EmptyState, ErrorBanner, MetricTile, PageHeader, PanelCard, PlatformBadge, SegmentedControl, Sparkline, StatusBadge } from '../components';
import type { StatusTone } from '../components';
import { copy } from '../copy';
import { getAds, getAutopilotStatus, getDaily, getExperiments, getSummary, stopAllAds } from '../api/screens';
import { entryTitle, getHistoryPage, noRevertHint, revertChange, type HistoryEntry } from '../api/history';
import { answerClientQuestion, getClientQuestions, parseClients, type ClientQuestion } from '../api/clients';
import { getPeriod, perDay, type PeriodChoice } from '../api/period';
import { useResource } from '../api/useResource';
import { useBusinessNow } from '../api/useBusinessNow';
import { useDemo } from '../api/useDemo';
import { getNotifications, readNotification, type AppNotification } from '../api/notifications';
import type { Ad, AdStatus, AdsResponse, Daily, Experiment, MarkingStatus, Summary } from '../api/types';
import { displayClock, displayDate, displayDateTime, justNow, wholeLeads } from '../format';
import { DemoNotice, NullableMoney, campaignStopped, experimentPeriod, fewAdsReason, platformName, shownStatus } from './StatsShared';
import { DemoPanel } from '../components/DemoPanel';
import { formatMoney } from '../components/Money';
import { getBusiness, resumeAutopilot } from '../api/settings';
import { PrelaunchEmpty, PrelaunchLink, beforeLaunch } from './Prelaunch';
import './Home.css';

const labels = copy.historyScreen;
const journalSize = 5;
const armLetters = ['А', 'Б'];
// Статус кампании — самый «живой» из статусов её объявлений: работает хоть одно — кампания работает.
const statusOrder: AdStatus[] = ['active', 'moderation', 'rejected', 'paused_owner', 'paused_auto', 'draft', 'archived'];
const kindTone: Record<HistoryEntry['kind'], StatusTone | 'accent'> = {
  pause: 'danger', resume: 'success', launch: 'success', budget: 'accent', text: 'info', image: 'info',
  experiment_verdict: 'info', marking: 'neutral', revert: 'neutral',
};
const experimentTone: Record<Experiment['status'], StatusTone> = { running: 'info', concluded: 'success', cancelled: 'neutral' };

function Notifications({ items, changed }: { items: AppNotification[]; changed: () => void }) {
  const [busy, setBusy] = useState<number>();
  const [error, setError] = useState('');
  async function read(id: number) {
    setBusy(id); setError('');
    try { await readNotification(id); changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(undefined); }
  }
  return <section className="home-notices" aria-labelledby="notifications-title">
    <h2 id="notifications-title" className="visually-hidden">{copy.home.notifications}</h2>
    {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    {items.map((item) => <article key={item.id} className={`home-notice home-notice--${item.level}`}>
      <p className="home-notice__text"><strong>{item.title}.</strong> {wholeLeads(item.text)}{' '}
        <time className="home-notice__time" dateTime={item.created_at}>{displayDateTime(item.created_at)}</time></p>
      <Button variant="secondary" size="small" disabled={busy !== undefined} onClick={() => void read(item.id)}>{copy.home.understood}</Button>
    </article>)}
  </section>;
}

function BudgetTile({ summary }: { summary: Summary }) {
  const { budget } = summary;
  return <div className="metric-tile metric-tile--wide home-budget">
    <p className="metric-tile__label">{copy.home.budgetTile}</p>
    <BudgetRing spentKopecks={budget.spent_kop} budgetKopecks={budget.budget_kop} daysRemaining={budget.days_remaining} />
    <p className="metric-tile__hint">{copy.home.budgetLeft(formatMoney(budget.remaining_kop))}</p>
  </div>;
}

// Цена обращения есть только в дни с обращениями: по двум таким дням тренда не видно.
const minPricePoints = 3;

function Tiles({ summary, daily }: { summary: Summary; daily: Daily['items'] | null }) {
  const window = summary.clients_window;
  const prices = daily?.map((day) => day.cost_per_lead_kop) ?? [];
  return <section className="metric-tiles home-tiles" aria-label={copy.statistics.total}>
    <BudgetTile summary={summary} />
    <MetricTile label={copy.home.leads} value={summary.leads.toLocaleString('ru-RU')}
      chart={daily && <Sparkline values={daily.map((day) => day.leads)} dashed={daily.map((day) => day.is_incomplete)} />}
      hint={copy.home.periodSpent(formatMoney(summary.cost_kop))} />
    <MetricTile label={copy.home.leadPrice} value={summary.cost_per_lead_kop === null ? '—' : formatMoney(summary.cost_per_lead_kop)}
      chart={prices.filter((price) => price !== null).length >= minPricePoints
        && <Sparkline values={prices} dashed={daily!.map((day) => day.is_incomplete)} connect tone="info" />}
      hint={summary.cost_per_lead_kop === null ? copy.screens.noPrice : copy.screens.dates(displayDate(summary.from), displayDate(summary.to))} />
    <MetricTile label={copy.home.clients} value={window.clients === null ? '—' : window.clients.toLocaleString('ru-RU')}
      hint={copy.home.clientsHint(window)} />
    <MetricTile label={copy.home.clientPrice} value={window.cost_per_client_kop === null ? '—' : formatMoney(window.cost_per_client_kop)}
      hint={window.cost_per_client_kop === null ? copy.screens.noPrice : copy.screens.dates(displayDate(window.from), displayDate(window.to))} />
  </section>;
}

// До запуска нулей и плоских графиков не рисуем: только бюджет, который задал владелец, и что появится после запуска.
function AfterLaunch({ summary }: { summary: Summary }) {
  return <section className="home-after" aria-labelledby="home-after-title">
    <h2 id="home-after-title">{copy.prelaunch.afterTitle}</h2>
    <p className="home-after__budget">{copy.prelaunch.afterBudget(formatMoney(summary.budget.budget_kop))}</p>
    <p className="muted">{copy.prelaunch.afterHint}</p>
  </section>;
}

/** Маркировка не подтверждена или площадка не приняла данные: до запуска честно говорим, что сделать. */
function MarkingNotice({ marking }: { marking: MarkingStatus }) {
  // Директ обязателен для запуска; VK напоминаем, только если данные для него сохраняли (передача была).
  const platforms = (['direct', 'vk'] as const).filter((platform) => platform === 'direct' || marking.transmission[platform] !== null);
  const manual = platforms.filter((platform) => marking.transmission[platform] === 'marking_manual_required');
  const unconfirmed = platforms.filter((platform) => !marking.confirmed[platform]);
  if (!manual.length && !unconfirmed.length) return null;
  return <section className="home-marking notice" aria-labelledby="home-marking-title">
    <h2 id="home-marking-title">{copy.marking.todoTitle}</h2>
    {manual.map((platform) => <div key={platform}>
      <p>{copy.marking.manualRequired(copy.setup.accounts[platform])}</p>
      <ol>{copy.marking.manualSteps[platform].map((step) => <li key={step}>{step}</li>)}</ol>
    </div>)}
    {manual.length > 0 && <p className="field__hint">{copy.marking.confirmAfterSteps}</p>}
    {unconfirmed.length > 0 && <p>{copy.marking.unconfirmed(unconfirmed.map((platform) => copy.setup.accounts[platform]).join(', '))}</p>}
    <div className="actions"><Link className="button button--secondary" to="/settings#marking">{copy.marking.openSettings}</Link></div>
  </section>;
}

function ClientQuestionRow({ question, answered }: { question: ClientQuestion; answered: () => void }) {
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const { number, error: invalid } = parseClients(value, question.leads_count);
  async function send(answer: number | 'unknown') {
    setBusy(true); setError(undefined);
    try { await answerClientQuestion(question.id, answer); answered(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); setBusy(false); }
  }
  return <form className="home-need__row" onSubmit={(event) => { event.preventDefault(); if (number !== undefined) void send(number); }}>
    <p className="home-need__text">{copy.clients.question(question.leads_count)}
      <small>{copy.clientsScreen.week(displayDate(question.week_start), displayDate(question.week_end))}. {copy.home.clientsWhy}</small></p>
    <input className="input mono home-need__input" inputMode="numeric" autoComplete="off" value={value}
      aria-label={copy.clientsScreen.countLabel} aria-describedby={`home-range-${question.id}${invalid ? ` home-invalid-${question.id}` : ''}`} aria-invalid={!!invalid}
      onChange={(event) => setValue(event.target.value)} />
    <Button type="submit" disabled={busy || number === undefined}>{busy ? copy.clientsScreen.sending : copy.clientsScreen.send}</Button>
    <Button variant="secondary" disabled={busy} onClick={() => void send('unknown')}>{copy.clients.unknown}</Button>
    <p className="visually-hidden" id={`home-range-${question.id}`}>{copy.clientsScreen.range(question.leads_count)}</p>
    {invalid && <p className="field__error home-need__error" id={`home-invalid-${question.id}`}>{invalid}</p>}
    {error && <div className="home-need__error"><ErrorBanner message={error} onDismiss={() => setError(undefined)} /></div>}
  </form>;
}

function campaignRows({ items: ads, campaigns }: AdsResponse) {
  const byCampaign = ads.reduce((map, ad) => map.set(ad.campaign_id, [...(map.get(ad.campaign_id) ?? []), ad]), new Map<number, Ad[]>());
  return [...byCampaign.entries()].map(([id, items]) => {
    const status = statusOrder.find((value) => items.some((ad) => shownStatus(ad, campaigns) === value)) ?? 'draft';
    const cost = items.reduce((sum, ad) => sum + ad.metrics.cost_kop, 0);
    const leads = items.reduce((sum, ad) => sum + ad.metrics.leads, 0);
    return { id, name: items[0].campaign_name, platform: items[0].platform, status, cost, leads, price: leads ? Math.round(cost / leads) : null,
      reason: status === 'active' || campaignStopped(id, campaigns) ? null : items.find((ad) => ad.status === status && ad.reason)?.reason ?? null };
  });
}

function Campaigns({ data, prelaunch }: { data: AdsResponse; prelaunch: { hasPlan: boolean } | null }) {
  if (!data.items.length && prelaunch) return <PrelaunchEmpty hasPlan={prelaunch.hasPlan} title={copy.home.noCampaigns} description={copy.prelaunch.campaigns} action={!prelaunch.hasPlan} />;
  if (!data.items.length) return <EmptyState title={copy.home.noCampaigns} description={copy.screens.noAdsHint} />;
  return <div className="table-scroll home-campaigns-box"><table className="table table--stack home-campaigns">
    <thead><tr><th scope="col">{copy.screens.rowLabels.campaign}</th><th scope="col" className="text">{copy.screens.status}</th>
      <th scope="col" className="home-campaigns__trend">{copy.home.trend}</th>
      <th scope="col">{copy.home.spent}</th><th scope="col">{copy.home.leads}</th><th scope="col">{copy.home.leadPrice}</th></tr></thead>
    <tbody>{campaignRows(data).map((row) => {
      const daily = data.campaigns.find((campaign) => campaign.id === row.id)?.daily ?? [];
      return <tr key={row.id} className={row.status === 'active' || row.status === 'moderation' ? undefined : 'home-campaigns__off'}>
      <td><strong className="home-campaigns__name">{row.name}</strong><PlatformBadge platform={row.platform} />
        {row.reason && <span className="home-campaigns__why">{row.reason}</span>}</td>
      <td className="text" data-label={copy.screens.status}><AdStatusBadge status={row.status} /></td>
      <td className="home-campaigns__trend" data-label={copy.home.trend}>
        <Sparkline values={daily.map((day) => day.cost_kop)} dashed={daily.map((day) => day.is_incomplete)} label={copy.home.trendLabel(row.name)} /></td>
      <td className="num" data-label={copy.home.spent}>{formatMoney(row.cost)}</td>
      <td className="num" data-label={copy.home.leads}>{row.leads.toLocaleString('ru-RU')}</td>
      <td className="num" data-label={copy.home.leadPrice}><NullableMoney value={row.price} /></td>
    </tr>;
    })}</tbody>
  </table></div>;
}

function Experiments({ items, quiet }: { items: Experiment[]; quiet: string | null }) {
  if (!items.length) return <EmptyState title={copy.screens.noExperiments} description={quiet ?? copy.screens.experimentHint} />;
  return <ul className="home-ab">{items.map((experiment) => <li key={experiment.id} className="home-ab__item">
    <div className="home-ab__head"><h3>{experiment.title}</h3>
      <StatusBadge tone={experimentTone[experiment.status]}>{copy.screens.experimentStates[experiment.status]}</StatusBadge></div>
    <p className="home-ab__question">{experiment.comparing}</p>
    <ol className="home-ab__arms">{experiment.arms.map((arm, index) => <li key={armLetters[index]}>
      <span className="home-ab__letter" aria-label={copy.ads.arm(armLetters[index])}>{armLetters[index]}</span>
      {arm.image && <img src={arm.image} alt={copy.ads.armImage(armLetters[index])} loading="lazy" />}
      <span>{arm.audience && <><strong>{copy.ads.armAudience(arm.audience)}</strong> </>}«{arm.title}». {arm.text}</span>
    </li>)}</ol>
    {experiment.status === 'running' && experiment.hypothesis !== 'audience' && <p className="muted">{copy.screens.remainingShows(experiment.remaining_impressions)}</p>}
    {experiment.conclusion && <p className="home-ab__verdict"><strong>{copy.ads.conclusion}: </strong>{experiment.conclusion}</p>}
    {experiment.conclusion && experimentPeriod(experiment) && <p className="muted">{experimentPeriod(experiment)}</p>}
  </li>)}</ul>;
}

function figures(entry: HistoryEntry): string[] {
  return (Object.keys(labels.numberLabels) as (keyof typeof labels.numberLabels)[])
    .filter((key) => typeof entry.evidence[key] === 'number')
    .map((key) => {
      const value = entry.evidence[key] as number;
      return `${labels.numberLabels[key]}: ${key.endsWith('_kop') ? formatMoney(value) : value.toLocaleString('ru-RU')}`;
    });
}

function Journal({ items, now, changed }: { items: HistoryEntry[]; now: number | null; changed: () => void }) {
  const [confirming, setConfirming] = useState<number>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  async function revert(id: number) {
    setBusy(true); setError(undefined);
    try { await revertChange(id); setConfirming(undefined); changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  if (!items.length) return <EmptyState title={copy.screens.noHistory} description={labels.emptyDescription} />;
  return <>
    {error && <div className="home-log__error"><ErrorBanner message={error} onDismiss={() => setError(undefined)} /></div>}
    <ol className="home-log">{items.map((entry) => {
      const numbers = figures(entry);
      // Отменить остановку — значит продолжить рекламу: так кнопка и подписана, как в истории.
      const action = entry.kind === 'pause'
        ? { button: labels.resume, title: labels.resumeTitle, yes: labels.resumeYes, no: labels.resumeNo, busy: labels.resuming }
        : { button: copy.history.undo, title: labels.confirmTitle, yes: labels.confirmYes, no: labels.confirmNo, busy: labels.reverting };
      return <li key={entry.id} className={`home-log__item home-log__item--${kindTone[entry.kind]}`}>
        <time className="home-log__time mono" dateTime={entry.created_at}>
          {justNow(entry.created_at, now) ? labels.justNow : <><span>{displayDate(entry.created_at).slice(0, 5)}</span> <span>{displayClock(entry.created_at)}</span></>}</time>
        <div className="home-log__body">
          <h3>{entryTitle(entry, items)}</h3>
          <p>{entry.reason_text}</p>
          {noRevertHint(entry) && <p className="muted">{noRevertHint(entry)}</p>}
          {numbers.length > 0 && <p className="home-log__numbers mono">{numbers.join(' · ')}</p>}
          <p className="home-log__meta">{labels.authors[entry.actor]}{entry.platform ? ` · ${platformName(entry.platform)}` : ''}</p>
          {entry.status in labels.statuses && <StatusBadge>{labels.statuses[entry.status as keyof typeof labels.statuses]}</StatusBadge>}
          {entry.can_revert && confirming !== entry.id && <div className="actions">
            <Button variant="secondary" size="small" icon={entry.kind === 'pause' ? 'play' : 'undo'} onClick={() => setConfirming(entry.id)}>{action.button}</Button></div>}
          {confirming === entry.id && <div className="home-log__confirm" role="group" aria-label={action.title}>
            <strong>{action.title}</strong>
            {entry.revert_warning && <p>{entry.revert_warning}</p>}
            <div className="actions"><Button size="small" disabled={busy} onClick={() => void revert(entry.id)}>{busy ? action.busy : action.yes}</Button>
              <Button variant="secondary" size="small" disabled={busy} onClick={() => setConfirming(undefined)}>{action.no}</Button></div>
          </div>}
        </div>
      </li>;
    })}</ol>
  </>;
}

function Sources({ summary }: { summary: Summary }) {
  const platforms = summary.sources.map((source) => source.platform);
  const demo = useDemo();
  return <footer className="home-sources" aria-label={copy.home.sourcesTitle}>
    <p>{summary.sources.length
      ? summary.sources.map((source) => copy.statistics.source(platformName(source.platform), displayDateTime(source.fetched_at))).join(' · ')
      : copy.screens.noSources} · {copy.statistics.provisional}</p>
    <div className="home-sources__links">
      {demo && platforms.length > 0 && <p>{copy.screens.compareDemo}</p>}
      {!demo && platforms.includes('direct') && <a href="https://direct.yandex.ru/" target="_blank" rel="noreferrer">{copy.screens.compareDirect}</a>}
      {!demo && platforms.includes('vk') && <a href="https://ads.vk.com/" target="_blank" rel="noreferrer">{copy.screens.compareVk}</a>}
    </div>
  </footer>;
}

/** Средний расход за прошедшие дни месяца (сегодня включительно) и бюджет месяца, разложенный на все его дни. */
function pace(summary: Summary) {
  const [year, month] = summary.budget.month.split('-').map(Number);
  const days = new Date(year, month, 0).getDate();
  const passed = days - summary.budget.days_remaining + 1;
  if (passed < 1 || summary.budget.budget_kop === 0) return null;
  return copy.home.pace(formatMoney(perDay(summary.budget.spent_kop, passed)), formatMoney(perDay(summary.budget.budget_kop, days)));
}

export function HomePage() {
  const [choice, setChoice] = useState<PeriodChoice>('month');
  const board = useResource(useCallback(async (signal: AbortSignal) => {
    const period = await getPeriod(choice, signal);
    // График — дополнение к плиткам: без ряда по дням плитки показываются без него.
    const [summary, daily] = await Promise.all([getSummary(period, signal), getDaily(period, signal).then((value) => value.items).catch(() => null)]);
    return { summary, daily };
  }, [choice]));
  const ads = useResource(useCallback(async (signal: AbortSignal) => getAds(signal, await getPeriod(choice, signal)), [choice]));
  const autopilot = useResource(getAutopilotStatus);
  const history = useResource(useCallback((signal: AbortSignal) => getHistoryPage({ platform: '', actor: '' }, journalSize, signal), []));
  const experiments = useResource(getExperiments);
  const questions = useResource(getClientQuestions);
  const notifications = useResource(getNotifications);
  const business = useResource(getBusiness);
  const [asking, setAsking] = useState(false);
  const [askingResume, setAskingResume] = useState(false);
  const stopLock = useRef(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string>();
  const [stopped, setStopped] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState('');
  const [resumeWarning, setResumeWarning] = useState<string | null>(null);
  const now = useBusinessNow();
  const status = autopilot.data;
  const summary = board.data?.summary;
  const prelaunch = status && beforeLaunch(status) ? { hasPlan: status.has_plan } : null;
  // Пока время бизнеса неизвестно, показываем время проверки, а не «N минут назад», и не судим о давности.
  const minutes = status?.last_check_at && now !== null ? Math.max(0, Math.floor((now - Date.parse(status.last_check_at)) / 60000)) : null;
  const stale = status?.status === 'running' && now !== null && (minutes === null || minutes > 15);
  const working = !status?.last_check_at ? copy.screens.heartbeatMissing
    : minutes === null ? copy.home.checkedAt(displayDateTime(status.last_check_at)) : copy.home.working(minutes);
  // Автопилот работает, а показов нет: все кампании остановлены (например, кабинет отключили) — «всё по плану» не пишем.
  const noShows = status?.status === 'running' && !!ads.data?.items.length
    && ads.data.items.every((ad) => !['active', 'moderation'].includes(shownStatus(ad, ads.data!.campaigns)));
  // День запуска: работающих объявлений ещё нет, площадки проверяют — баннер называет проверку, как и карточки.
  const allChecking = status?.status === 'running' && !!ads.data?.items.length
    && !ads.data.items.some((ad) => shownStatus(ad, ads.data!.campaigns) === 'active')
    && ads.data.items.some((ad) => shownStatus(ad, ads.data!.campaigns) === 'moderation');
  const statusText = noShows ? (status?.held_by_minimum ? copy.home.noShowsMinimum : copy.home.noShows) : allChecking ? copy.home.checkingAds : status?.status === 'running' ? working
    : status?.status === 'budget_stop' ? (status.auto_continue ? copy.home.exhausted : copy.screens.exhaustedPaused)
      : status?.status === 'draft' ? copy.screens.draft : copy.home.pausedLine;
  const platformTrouble = !!status?.needs_attention.some((item) => item.id === 'platform' || item.id.startsWith('revoked_'));
  const onTrack = status?.status === 'running' && !stale && Boolean(status.last_check_at) && !noShows && !allChecking && !platformTrouble;
  const tone = !status ? 'neutral' : stale ? 'danger' : status.status === 'running' && !noShows ? 'success' : 'warning';
  const question = questions.data?.items.find((item) => !item.answered_at);
  // Строка о бюджете дублирует строку состояния и плитку бюджета; от владельца она нужна, только когда реклама стоит до его решения.
  const waitsOwner = status?.status === 'draft' || (status?.status === 'budget_stop' && !status.auto_continue);
  const attention = status?.needs_attention.filter((item) => !(question && item.destination === 'clients') && (item.id !== 'budget' || waitsOwner)) ?? [];
  function reloadAll() { board.reload(); ads.reload(); autopilot.reload(); history.reload(); experiments.reload(); questions.reload(); notifications.reload(); }
  function askStop() { setStopError(undefined); setAsking(true); }
  function resume() {
    setResuming(true); setResumeError(''); setResumeWarning(null);
    void resumeAutopilot().then((warning) => { setResumeWarning(warning); setStopped(false); setAskingResume(false); autopilot.reload(); history.reload(); ads.reload(); })
      .catch((reason: unknown) => setResumeError(reason instanceof Error ? reason.message : copy.errors.generic))
      .finally(() => setResuming(false));
  }
  async function stop() {
    if (stopLock.current) return;
    stopLock.current = true; setStopping(true); setStopError(undefined);
    try { await stopAllAds(); setStopped(true); setAsking(false); autopilot.reload(); history.reload(); ads.reload(); }
    catch (error) { setStopError(error instanceof Error ? error.message : copy.errors.generic); }
    finally { stopLock.current = false; setStopping(false); }
  }
  return <div className="page-stack home">
    <PageHeader title={copy.home.title} description={copy.screens.intro} actions={prelaunch ? <PrelaunchLink hasPlan={prelaunch.hasPlan} />
      : (status || autopilot.error) && <>
      {/* Одно действие — одна кнопка: «Остановить» и «Пауза» рядом делали одно и то же. */}
      {/* «Продолжить» включил не все кампании: кнопка остаётся рядом с «Остановить», пока включать есть что. */}
      {(status?.status === 'paused' || status?.resume_pending)
        && <Button icon="play" onClick={() => { setResumeError(''); setAskingResume(true); }} disabled={resuming}>{resuming ? copy.home.resumingAll : copy.home.resumeAll}</Button>}
      {status?.status !== 'paused' && <Button variant="danger" icon="stop" onClick={askStop} disabled={stopping || stopped}>{copy.home.stopAll}</Button>}
    </>} />
    <DemoNotice />
    <DemoPanel changed={reloadAll} />
    <section className={`home-state home-state--${tone}`} aria-label={copy.screens.status}>
      <span className="home-state__light" aria-hidden="true" />
      <div className="home-state__text">
        {autopilot.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
        {status && <p>{onTrack && <><strong>{copy.home.onTrack}</strong>{' '}</>}
          {onTrack && summary && pace(summary) && <><span>{pace(summary)}</span>{' '}</>}
          <span className={onTrack ? 'home-state__check' : 'home-state__headline'}>{statusText}</span></p>}
        {status?.status === 'running' && status.first_decisions && <p className="home-state__note">{status.first_decisions}</p>}
      </div>
      {!prelaunch && <SegmentedControl dark label={copy.statistics.period} value={choice} onChange={setChoice}
        options={(Object.keys(copy.screens.periods) as PeriodChoice[]).map((value) => ({ value, label: copy.screens.periods[value], shortLabel: copy.screens.shortPeriods[value] }))} />}
    </section>
    {autopilot.error && <ErrorBanner message={autopilot.error} onRetry={autopilot.reload} />}
    {stale && <ErrorBanner message={copy.screens.heartbeatStale} />}
    {stopped && <p role="status" className="notice">{copy.screens.stopped}</p>}
    {resumeError && <ErrorBanner message={resumeError} onDismiss={() => setResumeError('')} />}
    {resumeWarning ? <div className="notice" role="status"><p>{resumeWarning}</p></div>
      : status?.resume_pending && <div className="notice" role="status"><p>{copy.home.resumeLeft}</p></div>}
    {board.loading && <div className="metric-tiles" aria-hidden="true">{[0, 1, 2, 3].map((index) => <div key={index} className={`metric-tile${index === 0 ? ' metric-tile--wide' : ''}`}>
      <span className="skeleton home-skeleton" /></div>)}</div>}
    {board.loading && <p className="loading visually-hidden" role="status">{copy.screens.loading}</p>}
    {board.error && <ErrorBanner message={board.error} onRetry={board.reload} />}
    {summary && prelaunch && <>
      {status?.marking && <MarkingNotice marking={status.marking} />}
      <AfterLaunch summary={summary} />
    </>}
    {summary && !prelaunch && <>
      <Tiles summary={summary} daily={board.data?.daily ?? null} />
      {summary.budget.spent_kop > summary.budget.reported_kop && <p className="muted home-fresher">{copy.budget.fresher(formatMoney(summary.budget.reported_kop))}</p>}
    </>}
    {(question || attention.length > 0) && <section className="home-need" aria-labelledby="home-need-title">
      <h2 id="home-need-title" className="home-need__tag"><span aria-hidden="true" />{copy.home.needYou}</h2>
      <div className="home-need__list">
        {question && <ClientQuestionRow key={question.id} question={question} answered={() => { questions.reload(); board.reload(); autopilot.reload(); }} />}
        {attention.map((item) => <div className="home-need__row" key={item.id}>
          <p className="home-need__text">{item.title}<small>{prelaunch && item.id === 'budget'
            ? (prelaunch.hasPlan ? copy.prelaunch.planHint : copy.prelaunch.setupHint) : item.description}</small></p>
          {prelaunch && item.id === 'budget' ? <PrelaunchLink hasPlan={prelaunch.hasPlan} variant="secondary" /> : <Link className="button button--secondary" to={`/${item.destination}`}>{item.id === 'budget' && status?.status === 'budget_stop' ? copy.home.changeBudget : copy.screens.open}</Link>}
        </div>)}
      </div>
    </section>}
    {/* Уведомления — дополнение к главной: если их нет или они не загрузились, блока нет. */}
    {notifications.data && notifications.data.length > 0 && <Notifications items={notifications.data} changed={notifications.reload} />}
    <div className="home-columns">
      <div className="home-columns__main">
        <PanelCard title={copy.home.campaigns} titleId="home-campaigns-title" flush
          action={<Link to="/ads">{copy.home.allAds} <span aria-hidden="true">→</span></Link>}>
          {ads.loading && <p className="loading home-panel-pad" role="status">{copy.screens.loading}</p>}
          {ads.error && <div className="home-panel-pad"><ErrorBanner message={ads.error} onRetry={ads.reload} /></div>}
          {ads.data && <Campaigns data={ads.data} prelaunch={prelaunch} />}
        </PanelCard>
        {/* До запуска сравнивать нечего: блок появится вместе с кампаниями. */}
        {!prelaunch && <PanelCard title={copy.ads.experiments} titleId="home-ab-title">
          {experiments.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
          {experiments.error && <ErrorBanner message={experiments.error} onRetry={experiments.reload} />}
          {experiments.data && <Experiments items={experiments.data.items}
            quiet={ads.data && business.data ? fewAdsReason(ads.data.items, ads.data.campaigns, business.data.avg_check_kop) : null} />}
        </PanelCard>}
      </div>
      <PanelCard title={copy.home.journal} titleId="home-log-title" flush
        action={<Link to="/history">{copy.home.allHistory} <span aria-hidden="true">→</span></Link>}>
        {history.loading && <p className="loading home-panel-pad" role="status">{copy.screens.loading}</p>}
        {history.error && <div className="home-panel-pad"><ErrorBanner message={history.error} onRetry={history.reload} /></div>}
        {history.data && <Journal items={history.data.items} now={now} changed={() => { history.reload(); autopilot.reload(); ads.reload(); board.reload(); }} />}
      </PanelCard>
    </div>
    {summary && !prelaunch && <Sources summary={summary} />}
    <ConfirmDialog open={asking} title={copy.screens.stopTitle} confirm={copy.screens.confirmStop} confirming={copy.screens.stopping} cancel={copy.screens.cancel}
      busy={stopping} error={stopError} onConfirm={() => void stop()} onClose={() => setAsking(false)}><p>{copy.screens.stopDescription}</p></ConfirmDialog>
    <ConfirmDialog open={askingResume} title={copy.home.resumeTitle} confirm={copy.home.resumeYes} confirming={copy.home.resumingAll} cancel={copy.home.resumeNo} tone="primary"
      busy={resuming} error={resumeError || undefined} onConfirm={resume} onClose={() => setAskingResume(false)}><p>{copy.home.resumeDescription}</p></ConfirmDialog>
  </div>;
}
