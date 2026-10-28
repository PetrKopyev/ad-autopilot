import { useEffect, useRef, useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router';
import { Button, Card, ErrorBanner, Stepper } from '../components';
import { copy } from '../copy';
import { checkAccounts, checkSite, connectAccount, createBusiness, disconnectAccount, getAuthConfig, getBudgetHint, getCounters, getGoals,
  getMe, rublesToKopecks, saveDescription, saveMarking, saveMetrica, validInn } from '../api/setup';
import type { AccountCheck, BudgetHint, Business, Counter, Description, DescriptionKey, Goal, Marking, SiteCheck, StopResult } from '../api/setup';
import { getBusiness } from '../api/settings';
import type { BusinessSettings, StoredMarking } from '../api/settings';
import { formatMoney } from '../components/Money';
import { useSession } from '../api/session';
import { NotFoundPage } from './PlaceholderPage';
import { MedicalNotice, SiteInstructions, StopNotice } from './SetupShared';
import './Setup.css';

interface Draft {
  user?: number; business?: Business; counter: number; goals: number[]; site?: SiteCheck; description?: Description;
  medical?: SiteCheck['medical_level']; approved: boolean; maxStep: number;
}
const storageKey = 'ad-autopilot-setup-draft';
const emptyDraft: Draft = { counter: 0, goals: [], approved: false, maxStep: 1 };
function readDraft(): Draft | null {
  try { const value = sessionStorage.getItem(storageKey); if (value) return JSON.parse(value) as Draft; } catch { /* Continue without browser storage. */ }
  return null;
}
// Черновика в этой вкладке нет: пройденные шаги восстанавливаются по сохранённому на сервере. Шаг сайта своего следа
// не оставляет — его проверка при входе на шаг берётся из кэша сервера.
function restoredDraft(saved: BusinessSettings, user: number): Draft {
  const { id, site_url, city, month_budget_kop, avg_check_kop } = saved;
  const counter = saved.metrica_counter_id ?? 0;
  const goals = counter ? saved.lead_goal_ids : [];
  return { user, business: { id, site_url, city, month_budget_kop, avg_check_kop }, counter, goals, description: saved.description?.fields,
    medical: saved.medical_level, approved: saved.description_approved,
    maxStep: saved.description_approved || saved.marking ? 5 : counter && goals.length ? 3 : 2 };
}
const emptyMarking: Marking = { subject_type: 'ip', inn: '', name: '', phone: '', confirmed_in_platform: { direct: false, vk: false } };
// ИНН и телефон сервер целиком не отдаёт: поля пустые, сохранённые остаются, пока реквизиты не меняют.
const markingForm = (stored: StoredMarking | null): Marking => stored
  ? { subject_type: stored.subject_type, inn: '', name: stored.name, phone: '', confirmed_in_platform: { ...emptyMarking.confirmed_in_platform, ...stored.confirmed_in_platform } }
  : emptyMarking;
const fields: DescriptionKey[] = ['services', 'prices', 'address', 'difference'];

export function SetupPage() {
  const { step } = useParams();
  const navigate = useNavigate();
  const [stored] = useState(readDraft);
  const [draft, setDraft] = useState(stored ?? emptyDraft);
  const [restoring, setRestoring] = useState(!stored?.business);
  const [website, setWebsite] = useState(draft.business?.site_url ?? '');
  const [city, setCity] = useState(draft.business?.city ?? '');
  const [budget, setBudget] = useState(draft.business ? String(draft.business.month_budget_kop / 100) : '');
  const [average, setAverage] = useState(draft.business?.avg_check_kop ? String(draft.business.avg_check_kop / 100) : '');
  const [noSite, setNoSite] = useState(false);
  const [budgetHint, setBudgetHint] = useState<BudgetHint>();
  const session = useSession();
  const me = session.data;
  const [counters, setCounters] = useState<Counter[]>([]);
  const [countersLoaded, setCountersLoaded] = useState(false);
  const [checks, setChecks] = useState<AccountCheck[]>();
  const [checking, setChecking] = useState(false);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [siteRetry, setSiteRetry] = useState(0);
  const handledRetry = useRef(0);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [stopResult, setStopResult] = useState<StopResult>();
  const [marking, setMarking] = useState(emptyMarking);
  const [savedMarking, setSavedMarking] = useState<StoredMarking | null>(null);
  const [innTouched, setInnTouched] = useState(false);
  const innError = validInn(marking.inn) && marking.inn.length === (marking.subject_type === 'ooo' ? 10 : 12) ? '' : copy.launchFlow.innError(marking.subject_type);
  const showInnError = (innTouched && !savedMarking || !!marking.inn) && !!innError;
  const keepMarking = !!savedMarking && !marking.inn && !marking.phone && marking.subject_type === savedMarking.subject_type && marking.name === savedMarking.name
    && (['direct', 'vk'] as const).every((platform) => marking.confirmed_in_platform[platform] === !!savedMarking.confirmed_in_platform[platform]);
  const current = Number(step ?? draft.maxStep);
  const direct = me?.accounts.find((account) => account.platform === 'direct' && account.status === 'ok');
  const vk = me?.accounts.find((account) => account.platform === 'vk' && account.status === 'ok');
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => {
      if (!config.demo || controller.signal.aborted) return;
      setWebsite((value) => value || 'https://salon.example/');
      setCity((value) => value || 'Москва');
      setBudget((value) => value || '30000');
    }).catch(() => {});
    return () => controller.abort();
  }, []);
  const userId = me?.id;
  const otherUser = userId !== undefined && draft.user !== undefined && draft.user !== userId;
  // Счётчик и цели спрашиваем только для черновика вошедшего пользователя, когда восстановление закончено.
  const ready = userId !== undefined && draft.user === userId && !restoring;
  // Черновик сменился целиком: поля формы и реквизиты прежнего черновика не остаются.
  function fill(value: Draft, saved: StoredMarking | null = null) {
    const business = value.business;
    setDraft(value);
    setWebsite(business?.site_url ?? ''); setCity(business?.city ?? ''); setBudget(business ? String(business.month_budget_kop / 100) : '');
    setAverage(business?.avg_check_kop ? String(business.avg_check_kop / 100) : '');
    setNoSite(false); setCounters([]); setCountersLoaded(false); setGoals([]); setSavedMarking(saved); setMarking(markingForm(saved)); setInnTouched(false); setChecks(undefined);
  }
  // Сессия пропадает на время перечитывания: сохранённое спрашиваем один раз на пользователя.
  const loadedFor = useRef<number>();
  useEffect(() => {
    if (userId === undefined || loadedFor.current === userId) return;
    const restore = restoring || otherUser;
    if (!restore) setDraft((value) => value.user === undefined ? { ...value, user: userId } : value);
    if (!me?.has_business) {
      loadedFor.current = userId;
      if (restore) { fill({ ...emptyDraft, user: userId }); setRestoring(false); }
      return;
    }
    if (restore) setRestoring(true);
    const controller = new AbortController();
    getBusiness(controller.signal).then((saved) => {
      if (controller.signal.aborted) return;
      loadedFor.current = userId;
      if (restore) { fill(restoredDraft(saved, userId), saved.marking); setRestoring(false); return; }
      setSavedMarking(saved.marking);
      setMarking((value) => value === emptyMarking ? markingForm(saved.marking) : value);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      const message = reason instanceof Error ? reason.message : copy.errors.generic;
      // Без сохранённого пустой первый шаг создал бы бизнес заново: формы нет, пока его не загрузим.
      if (restore) setLoadError(message); else setError(message);
    });
    return () => controller.abort();
  }, [userId, attempt]);
  // Расчёт к бюджету — с сервера, по тем же правилам, что план и запуск; пока сумму вводят, запрос ждёт паузы.
  const hintAmount = current === 1 ? rublesToKopecks(budget) : null;
  useEffect(() => {
    setBudgetHint(undefined);
    if (!hintAmount) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      getBudgetHint(hintAmount, !!vk, controller.signal).then((value) => { if (!controller.signal.aborted) setBudgetHint(value); }).catch(() => {});
    }, 300);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [hintAmount, vk]);
  useEffect(() => {
    if (restoring) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify(draft)); } catch { /* Keep the current form usable. */ }
  }, [draft, restoring]);
  useEffect(() => {
    const controller = new AbortController();
    if (direct && ready) getCounters(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setCounters(result.items); setCountersLoaded(true);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : copy.errors.generic); });
    return () => controller.abort();
  }, [direct, ready, revision]);
  useEffect(() => {
    if (current !== 2 || !direct || counters.length !== 1) return;
    setDraft((value) => value.counter === counters[0].id ? value
      : { ...value, counter: counters[0].id, goals: [], site: undefined, description: undefined, approved: false, maxStep: 2 });
  }, [counters, current, direct]);
  useEffect(() => {
    const controller = new AbortController(); setGoals([]);
    if (draft.counter && ready) getGoals(draft.counter, controller.signal).then((value) => {
      if (!controller.signal.aborted) setGoals(value.items);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : copy.errors.generic); });
    return () => controller.abort();
  }, [draft.counter, ready, revision]);
  useEffect(() => {
    if (current !== 3 || !draft.business || otherUser) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      setReading(true);
      try {
        const site = await checkSite(controller.signal, siteRetry !== handledRetry.current);
        if (controller.signal.aborted) return;
        handledRetry.current = siteRetry;
        // Ответ дополняет только пустые поля: введённое владельцем, даже несохранённое, не затирается.
        setDraft((value) => ({ ...value, site, description: site.status !== 'ready' ? value.description
          : Object.fromEntries(fields.map((key) => [key, value.description?.[key].value.trim() ? value.description[key]
            : { ...site.description[key], value: site.description[key].verified && site.description[key].quote ? site.description[key].value : '' }])) as Description }));
        if (site.status === 'reading') timer = setTimeout(() => void poll(), 1500);
        else setReading(false);
      } catch (reason) { if (!controller.signal.aborted) { handledRetry.current = siteRetry; setError(reason instanceof Error ? reason.message : copy.errors.generic); setReading(false); } }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [current, draft.business, otherUser, revision, siteRetry]);
  async function act(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError(''); setStopResult(undefined);
    try { await action(); } catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  // Проверка не перечитывает сессию: карточки кабинетов не пропадают на время запроса, итог — текстом под кнопкой.
  async function checkConnection() {
    if (checking) return; setChecking(true); setError('');
    try {
      const { items } = await checkAccounts(draft.business?.id);
      setChecks(items);
      if (items.some((item) => !item.ok)) session.update(await getMe());
      if (items.some((item) => item.platform === 'metrica' && item.ok)) {
        setCounters((await getCounters()).items); setCountersLoaded(true);
        if (draft.counter) setGoals((await getGoals(draft.counter)).items);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setChecking(false); }
  }
  function next(value: Partial<Draft> = {}) {
    setDraft((previous) => ({ ...previous, ...value, maxStep: Math.max(previous.maxStep, current + 1) }));
    navigate(`/setup/${current + 1}`);
  }
  async function submit() {
    if (current === 1) {
      const month_budget_kop = rublesToKopecks(budget); const avg_check_kop = average ? rublesToKopecks(average) : null;
      if (!month_budget_kop || (average && !avg_check_kop)) throw new Error(copy.launchFlow.moneyError);
      let url: URL;
      try { url = new URL(website); } catch { throw new Error(copy.launchFlow.businessError); }
      if (!['http:', 'https:'].includes(url.protocol) || !city.trim()) throw new Error(copy.launchFlow.businessError);
      const input = { site_url: url.href, city: city.trim(), month_budget_kop, avg_check_kop };
      if (draft.business && Object.entries(input).every(([key, value]) => draft.business![key as keyof Business] === value)) { next(); return; }
      const business = await createBusiness(input);
      session.reload(); setChecks(undefined);
      setDraft({ user: draft.user, business, counter: 0, goals: [], approved: false, maxStep: 2 }); navigate('/setup/2');
    } else if (current === 2) {
      const fresh = await getMe();
      if (!fresh.accounts.some((account) => account.platform === 'direct' && account.status === 'ok')
        || !counters.some((item) => item.id === draft.counter) || !draft.goals.length
        || draft.goals.some((id) => !goals.some((goal) => goal.id === id))) throw new Error(copy.launchFlow.accountsError);
      await saveMetrica(draft.counter, draft.goals); next();
    } else if (current === 3) {
      if (reading || draft.site?.status !== 'ready' || !draft.site.counter_id) throw new Error(copy.setup.website.blocked);
      if (draft.site.counter_id !== draft.counter) throw new Error(copy.launchFlow.counterMismatch);
      next();
    } else if (current === 4) {
      if (!draft.approved || !draft.description?.services.value.trim()) throw new Error(copy.launchFlow.draftError);
      await saveDescription(draft.description); next();
    } else {
      // Сохранённые реквизиты без правок не отправляются заново: менять их можно, только введя ИНН и телефон.
      if (!keepMarking) {
        setInnTouched(true);
        if (innError) throw new Error(innError);
      }
      if (!keepMarking && (!marking.name.trim() || !/^\+?[\d ()-]{10,25}$/.test(marking.phone) || marking.phone.replace(/\D/g, '').length < 10)
        || !marking.confirmed_in_platform.direct || (vk && !marking.confirmed_in_platform.vk)) throw new Error(copy.launchFlow.markingError);
      if (!keepMarking) await saveMarking(marking);
      navigate('/plan');
    }
  }
  if (loadError) return <div className="page-stack launch-flow">
    <h1>{copy.setup.title}</h1>
    <ErrorBanner message={loadError} onRetry={() => { setLoadError(''); setAttempt((value) => value + 1); }} />
  </div>;
  // Без бизнеса восстанавливать нечего: первый шаг открывается сразу.
  if (restoring && me?.has_business !== false || otherUser) return <p className="loading" role="status">{copy.screens.loading}</p>;
  if (step === undefined) return <Navigate to={`/setup/${Math.min(5, draft.maxStep)}`} replace />;
  if (!/^[1-5]$/.test(step)) return <NotFoundPage />;
  if (current > draft.maxStep) return <Navigate to={`/setup/${Math.min(5, draft.maxStep)}`} replace />;
  const blocked = current === 1 && noSite || current === 3 && !!draft.site && (draft.site.status === 'blocked' || draft.site.status === 'ready' && !draft.site.counter_id);
  const siteReady = draft.site?.status === 'ready' && !!draft.site.counter_id && draft.site.counter_id === draft.counter;
  return <div className="page-stack launch-flow">
    <h1>{copy.setup.title}</h1>
    <Stepper steps={copy.setup.steps.map((label, index) => ({ label, to: index + 1 < current ? `/setup/${index + 1}` : undefined }))} currentStep={current - 1} />
    {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    <Card><form className="page-stack" onSubmit={(event) => { event.preventDefault(); void act(submit); }}>
      <h2>{copy.setup.steps[current - 1]}</h2>
      <fieldset disabled={busy} className="page-stack">
      {current === 1 && <>
        <label className="check"><input type="checkbox" checked={noSite} onChange={(event) => setNoSite(event.target.checked)} />{copy.launchFlow.websiteMissing}</label>
        {!noSite && <>
          <label className="field">{copy.setup.business.website}<input type="url" required value={website} onChange={(event) => setWebsite(event.target.value)} /></label>
          <label className="field">{copy.setup.business.city}<input required value={city} onChange={(event) => setCity(event.target.value)} /></label>
          <label className="field">{copy.setup.business.budget}<input required inputMode="decimal" value={budget} aria-describedby="budget-hint" onChange={(event) => setBudget(event.target.value)} /></label>
          <div id="budget-hint" className="page-stack">
            <p className="muted">{copy.setup.business.budgetHint}</p>
            {budgetHint && hintAmount && <BudgetNotes hint={budgetHint} amount={hintAmount} />}
          </div>
          <label className="field">{copy.setup.business.visitPrice}<input inputMode="decimal" value={average} aria-describedby="average-hint" onChange={(event) => setAverage(event.target.value)} /></label>
          <p id="average-hint" className="muted">{copy.setup.business.visitPriceHint}</p>
        </>}
      </>}
      {current === 2 && <>
        {stopResult && <StopNotice result={stopResult} />}
        <div className="account-grid">{(['direct', 'vk'] as const).map((platform) => {
          const account = me?.accounts.find((item) => item.platform === platform);
          return <Card key={platform} className="page-stack">
            <h2>{copy.setup.accounts[platform]}</h2><p className="muted">{platform === 'direct' ? copy.setup.accounts.required : copy.setup.accounts.optional}</p>
            <p>{!account || account.status === 'revoked' ? copy.setup.accounts.disconnected : account.status === 'ok' ? copy.setup.accounts.connected(account.external_login) : copy.setup.accounts.expired}</p>
            {platform === 'direct' ? account?.status === 'ok' ? <p role="status">{copy.setup.accounts.directConnected}</p>
              : <Button onClick={() => void act(async () => { await connectAccount('direct', draft.business?.id); setChecks(undefined); session.reload(); })}>{copy.setup.accounts.connectDirect}</Button>
              : <Link className="button button--secondary" to="/connect/vk">{copy.launchFlow.vkConnect}</Link>}
            {account && account.status !== 'revoked' && <Button variant="secondary" onClick={() => void act(async () => { setStopResult(await disconnectAccount(account.id)); setDraft((value) => ({ ...value, ...(platform === 'direct' ? { counter: 0, goals: [] } : {}), maxStep: 2 })); setChecks(undefined); session.reload(); })}>{copy.launchFlow.disconnect}</Button>}
            <details><summary>{copy.setup.accounts.create}</summary><ol>{(platform === 'direct' ? copy.launchFlow.createDirect : copy.launchFlow.createVk).map((text) => <li key={text}>{text}</li>)}</ol></details>
          </Card>;
        })}</div>
        <div className="actions"><Button variant="secondary" aria-busy={checking || undefined} disabled={checking} onClick={() => void checkConnection()}>
          {checking ? <span className="loading">{copy.launchFlow.checking}</span> : copy.launchFlow.refresh}
        </Button></div>
        <div role="status" aria-live="polite">{checks && <ul className="check-results">{checks.map((item) => <li key={item.platform} className={item.ok ? undefined : 'check-failed'}>
          <strong>{copy.launchFlow.checkTitles[item.platform]}:</strong> {item.message}
        </li>)}</ul>}</div>
        <label className="field">{copy.setup.accounts.counter}<select disabled={!direct} required value={draft.counter || ''} onChange={(event) => setDraft((value) => ({ ...value, counter: Number(event.target.value), goals: [], site: undefined, description: undefined, approved: false, maxStep: 2 }))}>
          {!counters.length && <option value="">{direct && countersLoaded ? copy.launchFlow.noCounters : copy.common.noData}</option>}{counters.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}
        </select></label>
        <fieldset className="page-stack"><legend>{copy.setup.accounts.goal}</legend>{goals.map((goal) => <label className="check" key={goal.id}><input type="checkbox" checked={draft.goals.includes(goal.id)} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 2, goals: event.target.checked ? [...value.goals, goal.id] : value.goals.filter((id) => id !== goal.id) }))} />{goal.name}</label>)}</fieldset>
        <details><summary>{copy.setup.accounts.createGoal}</summary><ol>{copy.launchFlow.goalSteps.map((text) => <li key={text}>{text}</li>)}</ol></details>
        {direct && countersLoaded && !counters.length && <div className="notice"><p><strong>{copy.launchFlow.noCounters}</strong></p><p>{copy.launchFlow.createCounter}</p>
          <p>{copy.setup.website.blocked}</p><a href="https://metrika.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.metricaLink}</a></div>}
      </>}
      {current === 3 && <div className="page-stack" role="status" aria-live="polite">
        {reading && <p className="loading">{copy.setup.website.reading}</p>}
        {draft.site && <><p>{copy.launchFlow.pagesRead(draft.site.pages_read)}</p><p>{copy.setup.website.foundServices(draft.site.services_found)}</p><p>{draft.site.counter_id ? copy.setup.website.counterFound : copy.setup.website.counterMissing}</p></>}
        {!reading && !siteReady && !blocked && <div className="notice"><p>{draft.site?.counter_id ? copy.launchFlow.counterMismatch : copy.setup.website.blocked}</p><SiteInstructions /></div>}
        <Button variant="secondary" disabled={reading} onClick={() => { setError(''); setRevision((value) => value + 1); setSiteRetry((value) => value + 1); }}>{copy.common.retry}</Button>
      </div>}
      {blocked && <div className="notice"><h2>{copy.launchFlow.blockedTitle}</h2><p>{copy.setup.website.blocked}</p>{noSite && <p>{copy.launchFlow.createSite}</p>}<SiteInstructions /></div>}
      {current === 4 && draft.description && <>
        {(draft.site?.medical_level ?? draft.medical) !== 'none' && <MedicalNotice />}
        {!fields.some((key) => draft.description![key].verified) && <><h2>{copy.launchFlow.ownDescription}</h2><p>{copy.launchFlow.ownDescriptionHint}</p></>}
        {fields.map((key) => { const field = draft.description![key]; const sourced = field.verified && field.quote?.trim() === field.value.trim(); return <div className="field" key={key}>
          <label className="field">{copy.setup.draft[key]}<textarea required={key === 'services'} value={field.value} aria-describedby={[key === 'services' ? 'services-hint' : '', sourced ? '' : `source-${key}`].filter(Boolean).join(' ') || undefined} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: false, description: { ...value.description!, [key]: { ...field, value: event.target.value } } }))} /></label>
          {key === 'services' && <p className="muted" id="services-hint">{copy.setup.draft.servicesHint}</p>}
          {!sourced && <p className="source" id={`source-${key}`}>{field.verified && field.quote ? copy.setup.draft.source(field.quote) : `${copy.setup.draft.missing}. ${field.question || copy.setup.draft.question}`}</p>}
        </div>; })}
        <label className="check"><input type="checkbox" checked={draft.approved} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: event.target.checked }))} />{copy.launchFlow.approved}</label>
      </>}
      {current === 5 && <>
        <p>{copy.setup.advertiser.explanation}</p>
        <label className="field">{copy.setup.advertiser.type}<select value={marking.subject_type} onChange={(event) => setMarking((value) => ({ ...value, subject_type: event.target.value as Marking['subject_type'] }))}>{Object.entries(copy.launchFlow.subjectTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label className="field">{copy.setup.advertiser.taxId}<input required={!savedMarking} inputMode="numeric" maxLength={12} value={marking.inn} aria-invalid={showInnError} aria-describedby={showInnError ? 'inn-help' : undefined} onBlur={() => setInnTouched(true)} onChange={(event) => setMarking((value) => ({ ...value, inn: event.target.value.trim() }))} /></label>
        {showInnError && <p className="muted" id="inn-help">{innError}</p>}
        <label className="field">{copy.launchFlow.fullName}<input required value={marking.name} onChange={(event) => setMarking((value) => ({ ...value, name: event.target.value }))} /></label>
        <label className="field">{copy.launchFlow.phone}<input type="tel" required={!savedMarking} value={marking.phone} onChange={(event) => setMarking((value) => ({ ...value, phone: event.target.value }))} /></label>
        {savedMarking && <p className="muted">{copy.settingsScreen.markingStored(savedMarking.inn_masked, savedMarking.phone_masked)}</p>}
        <ol>{copy.launchFlow.markingSteps.map((text) => <li key={text}>{text}</li>)}</ol><p className="muted">{copy.launchFlow.markingOverwrite}</p>
        {(['direct', 'vk'] as const).filter((platform) => platform === 'direct' || vk).map((platform) => <label key={platform} className="check"><input type="checkbox" checked={marking.confirmed_in_platform[platform]} onChange={(event) => setMarking((value) => ({ ...value, confirmed_in_platform: { ...value.confirmed_in_platform, [platform]: event.target.checked } }))} />{platform === 'direct' ? copy.launchFlow.confirmDirect : copy.launchFlow.confirmVk}</label>)}
      </>}
      </fieldset>
      <div className="actions">
        {current > 1 && <Link className="button button--secondary" to={`/setup/${current - 1}`}>{copy.common.back}</Link>}
        <Button type="submit" aria-describedby={current === 4 && !draft.approved ? 'approval-help' : undefined} disabled={busy || blocked || current === 3 && (reading || !siteReady) || current === 4 && !draft.approved}>{busy ? copy.launchFlow.saving : current === 5 ? copy.plan.title : copy.common.next}</Button>
      </div>
      {current === 4 && !draft.approved && <p className="muted" id="approval-help">{copy.launchFlow.approvalHint}</p>}
    </form></Card>
  </div>;
}

function BudgetNotes({ hint, amount }: { hint: BudgetHint; amount: number }) {
  const { direct, vk } = hint.daily_kop;
  if (amount < hint.minimum_kop) return <div className="notice" role="alert"><p>{copy.setup.business.budgetMinimum(formatMoney(hint.minimum_kop))}</p></div>;
  return <>
    {direct !== undefined && <p>{copy.setup.business.budgetDaily(formatMoney(direct), vk === undefined ? undefined : formatMoney(vk))}</p>}
    {hint.off.map((item) => <p key={item.platform}>{copy.setup.business.budgetVkOff(formatMoney(item.from_kop), formatMoney(item.min_daily_kop))}</p>)}
    {amount < hint.low_kop && <div className="notice" role="status"><p>{copy.setup.business.budgetLow(formatMoney(hint.low_kop))}</p></div>}
  </>;
}
