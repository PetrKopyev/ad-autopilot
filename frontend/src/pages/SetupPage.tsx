import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router';
import { Button, Card, CityCombobox, ErrorBanner, Icon, PageHeader, PhoneInput, RublesInput, StatusBadge, Stepper, describedBy, phoneDigits, rublesValue } from '../components';
import type { StatusTone } from '../components';
import { copy } from '../copy';
import { checkAccounts, checkSite, connectAccount, createBusiness, disconnectAccount, getAuthConfig, getBudgetHint, getCities, getCounters, getGoals,
  getMe, moneyError, rublesToKopecks, saveDescription, saveMarking, saveMetrica, tooMuchMoney, validInn } from '../api/setup';
import type { Account, AccountCheck, BudgetHint, Business, Counter, Description, DescriptionKey, Goal, Marking, SiteCheck, StopResult } from '../api/setup';
import { dailyAverage } from '../api/period';
import { getBusiness } from '../api/settings';
import type { BusinessSettings, StoredMarking } from '../api/settings';
import { formatMoney } from '../components/Money';
import { useSession } from '../api/session';
import { cityKey } from '../components/CityCombobox';
import { NotFoundPage } from './PlaceholderPage';
import { MedicalNotice, SetupField, SiteInstructions, StopNotice } from './SetupShared';
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
const ownerFields: DescriptionKey[] = ['services', 'address', 'difference'];
// Ошибка одного поля: показывается под ним, а не общей полосой; field — id поля.
class FieldError extends Error {
  constructor(readonly field: string, message: string) { super(message); }
}

export function SetupPage() {
  const { step } = useParams();
  const navigate = useNavigate();
  const [stored] = useState(readDraft);
  const [draft, setDraft] = useState(stored ?? emptyDraft);
  const [restoring, setRestoring] = useState(!stored?.business);
  const [website, setWebsite] = useState(draft.business?.site_url ?? '');
  const [city, setCity] = useState(draft.business?.city ?? '');
  const [budget, setBudget] = useState(draft.business ? rublesValue(draft.business.month_budget_kop) : '');
  const [average, setAverage] = useState(draft.business?.avg_check_kop ? rublesValue(draft.business.avg_check_kop) : '');
  const [noSite, setNoSite] = useState(false);
  const [demo, setDemo] = useState(false);
  const [budgetHint, setBudgetHint] = useState<BudgetHint>();
  const [cities, setCities] = useState<string[]>();
  const session = useSession();
  const me = session.data;
  const [counters, setCounters] = useState<Counter[]>([]);
  const [countersLoaded, setCountersLoaded] = useState(false);
  const [checks, setChecks] = useState<AccountCheck[]>();
  const [checking, setChecking] = useState(false);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [goalsFor, setGoalsFor] = useState(0);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [siteRetry, setSiteRetry] = useState(0);
  const handledRetry = useRef(0);
  const [error, setError] = useState('');
  const [fieldError, setFieldError] = useState<FieldError>();
  const [loadError, setLoadError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [stopResult, setStopResult] = useState<StopResult>();
  const [marking, setMarking] = useState(emptyMarking);
  const [savedMarking, setSavedMarking] = useState<StoredMarking | null>(null);
  const [innTouched, setInnTouched] = useState(false);
  const innError = validInn(marking.inn) && marking.inn.length === (marking.subject_type === 'ooo' ? 10 : 12) ? '' : copy.launchFlow.innError(marking.subject_type);
  const showInnError = (innTouched && !savedMarking || !!marking.inn) && !!innError;
  // «+7 » без цифр — как пустое: тронутый фокусом телефон сохранённые реквизиты не перезаписывает.
  const keepMarking = !!savedMarking && !marking.inn && !phoneDigits(marking.phone).length && marking.subject_type === savedMarking.subject_type && marking.name === savedMarking.name
    && (['direct', 'vk'] as const).every((platform) => marking.confirmed_in_platform[platform] === !!savedMarking.confirmed_in_platform[platform]);
  const current = Number(step ?? draft.maxStep);
  const direct = me?.accounts.find((account) => account.platform === 'direct' && account.status === 'ok');
  const vk = me?.accounts.find((account) => account.platform === 'vk' && account.status === 'ok');
  const anyAccount = !!me?.accounts.some((account) => account.status !== 'revoked');
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => {
      if (!controller.signal.aborted) setDemo(config.demo === true);
    }).catch(() => {});
    void getCities(controller.signal).then((value) => { if (!controller.signal.aborted) setCities(value.cities); }).catch(() => {});
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
    setWebsite(business?.site_url ?? ''); setCity(business?.city ?? ''); setBudget(business ? rublesValue(business.month_budget_kop) : '');
    setAverage(business?.avg_check_kop ? rublesValue(business.avg_check_kop) : '');
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
  // Демо предзаполняет пустые поля первого шага. Ждём восстановления черновика: оно обнуляет поля свежего входа,
  // и предзаполнение до него было бы стёрто; «только пустые» не затирает ни введённое владельцем, ни сохранённое.
  useEffect(() => {
    if (!demo || restoring) return;
    setWebsite((value) => value || 'https://salon.example/');
    setCity((value) => value || 'Москва');
    setBudget((value) => value || '50000');
    setMarking((value) => savedMarking || value.inn || value.name || value.phone ? value
      : { ...value, subject_type: 'ip', inn: '001234567887', name: 'Демо-салон', phone: '+7 (900) 123-45-67' });
  }, [demo, restoring, savedMarking]);
  // Расчёт к бюджету — с сервера, по тем же правилам, что план и запуск; пока сумму вводят, запрос ждёт паузы.
  const typedBudget = current === 1 ? rublesToKopecks(budget) : null;
  const hintAmount = tooMuchMoney(typedBudget) ? null : typedBudget;
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
      if (!controller.signal.aborted) { setGoals(value.items); setGoalsFor(draft.counter); }
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
    if (busy) return; setBusy(true); setError(''); setFieldError(undefined); setStopResult(undefined);
    try { await action(); } catch (reason) {
      if (reason instanceof FieldError) setFieldError(reason); else setError(reason instanceof Error ? reason.message : copy.errors.generic);
    } finally { setBusy(false); }
  }
  // Ошибка поля держится, пока его не поправят; курсор сразу ставим в поле с ошибкой.
  useEffect(() => setFieldError(undefined), [current, website, city, budget, average, noSite, draft.counter, draft.goals, draft.description, marking]);
  // Ответ сервера про прежние сведения о бизнесе после правки уже неправда: баннер не висит рядом с новым расчётом.
  useEffect(() => setError(''), [website, city, budget, average, noSite]);
  useEffect(() => { if (fieldError) document.getElementById(fieldError.field)?.focus(); }, [fieldError]);
  const errorFor = (field: string) => fieldError?.field === field ? fieldError.message : undefined;
  // Проверка не перечитывает сессию: карточки кабинетов не пропадают на время запроса, итог — текстом под кнопкой.
  async function checkConnection() {
    if (checking) return; setChecking(true); setError('');
    try {
      const { items } = await checkAccounts(draft.business?.id);
      setChecks(items);
      if (items.some((item) => !item.ok)) session.update(await getMe());
      if (items.some((item) => item.platform === 'metrica' && item.ok)) {
        setCounters((await getCounters()).items); setCountersLoaded(true);
        if (draft.counter) { setGoals((await getGoals(draft.counter)).items); setGoalsFor(draft.counter); }
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
      let url: URL;
      try { url = new URL(website); } catch { throw new FieldError('website', copy.setup.business.websiteError); }
      if (!['http:', 'https:'].includes(url.protocol)) throw new FieldError('website', copy.setup.business.websiteError);
      const chosen = cities ? cities.find((item) => cityKey(item) === cityKey(city)) : city.trim();
      if (!chosen) throw new FieldError('city', copy.setup.business.cityError);
      if (!month_budget_kop) throw new FieldError('budget', budget.trim() ? moneyError(budget) : copy.setup.business.budgetMissing);
      if (tooMuchMoney(month_budget_kop)) throw new FieldError('budget', copy.launchFlow.moneyTooBig);
      // Минимум проверяем здесь, даже если расчёт под полем ещё не пришёл: иначе тот же текст пришёл бы с сервера
      // баннером, а через миг — и под полем. Расчёт не получили — минимум проверит сервер.
      const hint = budgetHint ?? await getBudgetHint(month_budget_kop, !!vk).catch(() => undefined);
      if (hint && month_budget_kop < hint.minimum_kop) throw new FieldError('budget', copy.setup.business.budgetMinimum(formatMoney(hint.minimum_kop)));
      if (average && !avg_check_kop) throw new FieldError('average', moneyError(average));
      if (tooMuchMoney(avg_check_kop)) throw new FieldError('average', copy.launchFlow.moneyTooBig);
      const input = { site_url: url.href, city: chosen, month_budget_kop, avg_check_kop };
      if (draft.business && Object.entries(input).every(([key, value]) => draft.business![key as keyof Business] === value)) { next(); return; }
      const business = await createBusiness(input);
      session.reload(); setChecks(undefined);
      setDraft({ user: draft.user, business, counter: 0, goals: [], approved: false, maxStep: 2 }); navigate('/setup/2');
    } else if (current === 2) {
      const fresh = await getMe();
      if (!fresh.accounts.some((account) => account.platform === 'direct' && account.status === 'ok')) throw new Error(copy.launchFlow.accountsError);
      if (!counters.some((item) => item.id === draft.counter)) throw new FieldError('counter', copy.setup.accounts.counterError);
      if (!draft.goals.length || draft.goals.some((id) => !goals.some((goal) => goal.id === id))) throw new FieldError('goals', copy.setup.accounts.goalsError);
      await saveMetrica(draft.counter, draft.goals); next();
    } else if (current === 3) {
      if (reading || draft.site?.status !== 'ready' || !draft.site.counter_id) throw new Error(copy.setup.website.blocked);
      if (draft.site.counter_id !== draft.counter) throw new Error(copy.launchFlow.counterMismatch);
      next();
    } else if (current === 4) {
      if (!draft.description?.services.value.trim()) throw new FieldError('services', copy.setup.draft.servicesError);
      if (!draft.approved) throw new Error(copy.launchFlow.draftError);
      await saveDescription({ ...draft.description, prices: { ...draft.description.prices, value: '' } }); next();
    } else {
      // Сохранённые реквизиты без правок не отправляются заново: менять их можно, только введя ИНН и телефон.
      if (!keepMarking) {
        setInnTouched(true);
        if (innError) throw new FieldError('inn', innError);
        if (!marking.name.trim()) throw new FieldError('name', copy.setup.advertiser.nameError);
        if (phoneDigits(marking.phone).length !== 10) throw new FieldError('phone', copy.setup.advertiser.phoneError);
      }
      if (!marking.confirmed_in_platform.direct || (vk && !marking.confirmed_in_platform.vk)) throw new FieldError('confirm', copy.setup.advertiser.confirmError);
      if (!keepMarking) await saveMarking(marking);
      navigate('/plan');
    }
  }
  const header = (actions?: ReactNode) => <PageHeader title={copy.setup.title} actions={actions} />;
  if (loadError) return <div className="page-stack launch-flow setup">
    {header()}
    <ErrorBanner message={loadError} onRetry={() => { setLoadError(''); setAttempt((value) => value + 1); }} />
  </div>;
  // Без бизнеса восстанавливать нечего: первый шаг открывается сразу.
  if (restoring && me?.has_business !== false || otherUser) return <div className="page-stack launch-flow setup">
    {header()}<p className="loading" role="status">{copy.screens.loading}</p>
  </div>;
  if (step === undefined) return <Navigate to={`/setup/${Math.min(5, draft.maxStep)}`} replace />;
  if (!/^[1-5]$/.test(step)) return <NotFoundPage />;
  if (current > draft.maxStep) return <Navigate to={`/setup/${Math.min(5, draft.maxStep)}`} replace />;
  const blocked = current === 1 && noSite || current === 3 && !!draft.site && (draft.site.status === 'blocked' || draft.site.status === 'ready' && !draft.site.counter_id);
  const siteReady = draft.site?.status === 'ready' && !!draft.site.counter_id && draft.site.counter_id === draft.counter;
  const metricaReady = !!draft.counter && draft.goals.length > 0;
  const confirmed = marking.confirmed_in_platform.direct && (!vk || marking.confirmed_in_platform.vk);
  const innMessage = errorFor('inn') ?? (showInnError ? innError : undefined);
  const invalid = (message?: string) => message ? true : undefined;
  const fieldErrorText = (field: string) => errorFor(field) && <p id={`${field}-error`} className="field__error" role="alert">{errorFor(field)}</p>;
  return <div className="page-stack launch-flow setup">
    {header(<Stepper steps={copy.setup.stepsShort.map((label, index) => ({ label, to: index + 1 < current ? `/setup/${index + 1}` : undefined }))} currentStep={current - 1} />)}
    <Card className="setup__card"><form noValidate onSubmit={(event) => { event.preventDefault(); void act(submit); }}>
      <header className="setup__head">
        <p className="setup__eyebrow">{copy.setup.stepOf(current, copy.setup.steps.length)}</p>
        <h2>{copy.setup.steps[current - 1]}</h2>
        <p className="setup__intro">{current === 5 ? copy.setup.advertiser.explanation : copy.setup.intro[current - 1]}</p>
      </header>
      <fieldset disabled={busy} className="setup__body">
      {current === 1 && <>
        <label className="check"><input type="checkbox" checked={noSite} onChange={(event) => setNoSite(event.target.checked)} />{copy.launchFlow.websiteMissing}</label>
        {!noSite && <>
          <SetupField id="website" label={copy.setup.business.website} hint={copy.setup.business.websiteHint} error={errorFor('website')}>
            <input id="website" type="url" required placeholder="https://" value={website} aria-invalid={invalid(errorFor('website'))}
              aria-describedby={describedBy('website', true, errorFor('website'))} onChange={(event) => setWebsite(event.target.value)} />
          </SetupField>
          <CityCombobox id="city" label={copy.setup.business.city} hint={copy.setup.business.cityHint} error={errorFor('city')} cities={cities ?? []} value={city} onChange={setCity} required />
          <SetupField id="budget" label={copy.setup.business.budget} error={errorFor('budget')}
            hint={<><p>{copy.setup.business.budgetHint}</p>{budgetHint && hintAmount && !errorFor('budget') && <BudgetNotes hint={budgetHint} amount={hintAmount} />}</>}>
            <RublesInput id="budget" className="mono" required value={budget} aria-invalid={invalid(errorFor('budget'))}
              aria-describedby={describedBy('budget', true, errorFor('budget'))} onChange={setBudget} />
          </SetupField>
          <SetupField id="average" label={copy.setup.business.visitPrice} hint={copy.setup.business.visitPriceHint} error={errorFor('average')}>
            <RublesInput id="average" className="mono" value={average} aria-invalid={invalid(errorFor('average'))}
              aria-describedby={describedBy('average', true, errorFor('average'))} onChange={setAverage} />
          </SetupField>
        </>}
      </>}
      {current === 2 && <>
        <section className="setup__block" aria-labelledby="cabinets-title">
          <h3 id="cabinets-title">{copy.setup.accounts.cabinets}</h3>
          {stopResult && <StopNotice result={stopResult} />}
          <div className="account-grid">{(['direct', 'vk'] as const).map((platform) => {
            const account = me?.accounts.find((item) => item.platform === platform);
            const live = account && account.status !== 'revoked' ? account : undefined;
            const badge: [StatusTone, string] = live?.status === 'ok' ? ['success', copy.setup.accounts.ready] : live ? ['warning', copy.setup.accounts.expiredBadge]
              : platform === 'direct' ? ['warning', copy.setup.accounts.needed] : ['neutral', copy.setup.accounts.notConnected];
            return <div key={platform} className="account-card">
              <div className="setup__block-head"><h4>{copy.setup.accounts[platform]}</h4><StatusBadge tone={badge[0]}>{badge[1]}</StatusBadge></div>
              <p className="muted">{platform === 'direct' ? copy.setup.accounts.required : copy.setup.accounts.optional}</p>
              {live && <p>{live.status === 'ok' ? connectedAs(live) : copy.setup.accounts.expired}</p>}
              <div className="actions">
                {platform === 'direct' ? account?.status !== 'ok'
                  && <Button onClick={() => void act(async () => { await connectAccount('direct', draft.business?.id); setChecks(undefined); session.reload(); })}>{copy.setup.accounts.connectDirect}</Button>
                  : account?.status === 'ok' ? <Link className="tap-link" to="/connect/vk">{copy.setup.accounts.reconnect}</Link>
                    : <Link className="button button--secondary" to="/connect/vk">{copy.launchFlow.vkConnect}</Link>}
                {live && <Button variant="secondary" onClick={() => void act(async () => { setStopResult(await disconnectAccount(live.id)); setDraft((value) => ({ ...value, ...(platform === 'direct' ? { counter: 0, goals: [] } : {}), maxStep: 2 })); setChecks(undefined); session.reload(); })}>{copy.launchFlow.disconnect}</Button>}
              </div>
              <details><summary>{copy.setup.accounts.create}</summary><ol>{(platform === 'direct' ? copy.launchFlow.createDirect : copy.launchFlow.createVk).map((text) => <li key={text}>{text}</li>)}</ol></details>
            </div>;
          })}</div>
          <div className="actions"><Button variant="secondary" icon={checking ? undefined : 'shield'} aria-busy={checking || undefined} disabled={checking || !anyAccount} aria-describedby={anyAccount ? undefined : 'refresh-hint'} onClick={() => void checkConnection()}>
            {checking ? <span className="loading">{copy.launchFlow.checking}</span> : copy.launchFlow.refresh}
          </Button>{!anyAccount && <p className="field__hint" id="refresh-hint">{copy.launchFlow.refreshHint}</p>}</div>
          <div role="status" aria-live="polite">{checks && <ul className="check-results">{checks.map((item) => <li key={item.platform} className={item.ok ? undefined : 'check-failed'}>
            <strong>{copy.launchFlow.checkTitles[item.platform]}:</strong> {item.message}
          </li>)}</ul>}</div>
        </section>
        <section className="setup__block" aria-labelledby="metrica-title">
          <div className="setup__block-head"><h3 id="metrica-title">{copy.setup.accounts.metrica}</h3>
            <StatusBadge tone={metricaReady ? 'success' : 'warning'}>{metricaReady ? copy.setup.accounts.chosen : copy.setup.accounts.choose}</StatusBadge></div>
          <SetupField id="counter" label={copy.setup.accounts.counter} hint={copy.setup.accounts.counterHint} error={errorFor('counter')}>
            <select id="counter" disabled={!direct} required value={draft.counter || ''} aria-invalid={invalid(errorFor('counter'))} aria-describedby={describedBy('counter', true, errorFor('counter'))}
              onChange={(event) => setDraft((value) => ({ ...value, counter: Number(event.target.value), goals: [], site: undefined, description: undefined, approved: false, maxStep: 2 }))}>
              {!counters.length && <option value="">{direct && countersLoaded ? copy.launchFlow.noCounters : copy.common.noData}</option>}{counters.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}
            </select>
          </SetupField>
          {goals.length ? <fieldset id="goals" className="setup__goals" aria-describedby={describedBy('goals', true, errorFor('goals'))}>
            <legend>{copy.setup.accounts.goal}</legend>
            <p id="goals-hint" className="field__hint">{copy.setup.accounts.goalHint}</p>
            {goals.map((goal) => <label className="check" key={goal.id}><input type="checkbox" checked={draft.goals.includes(goal.id)} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 2, goals: event.target.checked ? [...value.goals, goal.id] : value.goals.filter((id) => id !== goal.id) }))} />{goal.name}</label>)}
          </fieldset>
            : <div className="field"><strong className="field__label">{copy.setup.accounts.goal}</strong><p className="field__hint">{direct && draft.counter && goalsFor === draft.counter ? copy.setup.accounts.goalsNone : copy.setup.accounts.goalsFirst}</p></div>}
          {fieldErrorText('goals')}
          <details><summary>{copy.setup.accounts.createGoal}</summary><ol>{copy.launchFlow.goalSteps.map((text) => <li key={text}>{text}</li>)}</ol></details>
          {direct && countersLoaded && !counters.length && <div className="notice"><p><strong>{copy.launchFlow.noCounters}</strong></p><p>{copy.launchFlow.createCounter}</p>
            <p>{copy.setup.website.blocked}</p><a href="https://metrika.yandex.ru/" target="_blank" rel="noreferrer">{copy.launchFlow.metricaLink}</a></div>}
        </section>
      </>}
      {current === 3 && <>
        <div className="site-check" role="status" aria-live="polite">
          {reading && <p className="loading">{copy.setup.website.reading}</p>}
          {draft.site && <dl className="site-check__rows">
            <div><dt>{copy.setup.website.site}</dt><dd>{copy.launchFlow.pagesRead(draft.site.pages_read)}</dd></div>
            <div><dt>{copy.setup.website.services}</dt><dd>{copy.setup.website.foundServices(draft.site.services_found)}</dd></div>
            <div><dt>{copy.setup.website.counter}</dt><dd><StatusBadge tone={!draft.site.counter_id ? 'danger' : siteReady ? 'success' : 'warning'}>
              {draft.site.counter_id ? copy.setup.website.counterFound : copy.setup.website.counterMissing}</StatusBadge></dd></div>
          </dl>}
          {!!draft.site?.services?.length && <ul className="found-services">{draft.site.services.map((item) => <li key={item.name}>
            <span>{item.name}</span>{item.price_kop !== null && <><span className="visually-hidden"> — </span><span className="mono">{formatMoney(item.price_kop)}</span></>}
          </li>)}</ul>}
        </div>
        {!reading && !siteReady && !blocked && <div className="notice"><p>{draft.site?.counter_id ? copy.launchFlow.counterMismatch : copy.setup.website.blocked}</p><SiteInstructions /></div>}
        <Button variant="secondary" className={siteReady ? 'retry-small' : ''} disabled={reading} onClick={() => { setError(''); setRevision((value) => value + 1); setSiteRetry((value) => value + 1); }}>{copy.common.retry}</Button>
      </>}
      {blocked && <div className="notice"><h3>{copy.launchFlow.blockedTitle}</h3><p>{copy.setup.website.blocked}</p>{noSite && <p>{copy.launchFlow.createSite}</p>}<SiteInstructions /></div>}
      {current === 4 && draft.description && <>
        {(draft.site?.medical_level ?? draft.medical) !== 'none' && <MedicalNotice />}
        {!fields.some((key) => draft.description![key].verified) && <div className="notice"><h3>{copy.launchFlow.ownDescription}</h3><p>{copy.launchFlow.ownDescriptionHint}</p></div>}
        {ownerFields.map((key) => {
          const field = draft.description![key];
          const sourced = field.verified && field.quote?.trim() === field.value.trim();
          const message = errorFor(key);
          return <div className={`field${message ? ' field--invalid' : ''}`} key={key}>
            <div className="setup__label-row"><label htmlFor={key} className="field__label">{copy.setup.draft[key]}</label>
              {sourced && <span className="source-badge"><Icon name="check" />{copy.setup.draft.fromSite}</span>}</div>
            <textarea id={key} required={key === 'services'} value={field.value} aria-invalid={invalid(message)}
              aria-describedby={[key === 'services' ? 'services-hint' : '', sourced ? '' : `source-${key}`, message ? `${key}-error` : ''].filter(Boolean).join(' ') || undefined}
              onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: false, description: { ...value.description!, [key]: { ...field, value: event.target.value } } }))} />
            {key === 'services' && <p className="field__hint" id="services-hint">{copy.setup.draft.servicesHint}</p>}
            {!sourced && <p className={field.verified && field.quote ? 'source source--quote' : 'source'} id={`source-${key}`}>
              {field.verified && field.quote ? copy.setup.draft.source(field.quote) : `${copy.setup.draft.missing}. ${field.question || copy.setup.draft.question}`}</p>}
            {fieldErrorText(key)}
          </div>;
        })}
        <div className="setup__confirm">
          <label className="check"><input type="checkbox" checked={draft.approved} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: event.target.checked }))} />{copy.launchFlow.approved}</label>
          {!draft.approved && <p className="field__hint" id="approval-help">{copy.launchFlow.approvalHint}</p>}
        </div>
      </>}
      {current === 5 && <>
        <SetupField id="subject-type" label={copy.setup.advertiser.type}>
          <select id="subject-type" value={marking.subject_type} onChange={(event) => setMarking((value) => ({ ...value, subject_type: event.target.value as Marking['subject_type'] }))}>
            {Object.entries(copy.launchFlow.subjectTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </SetupField>
        <SetupField id="inn" label={copy.setup.advertiser.taxId} hint={copy.setup.advertiser.taxIdHint(marking.subject_type === 'ooo' ? 10 : 12)} error={innMessage}>
          <input id="inn" className="mono" required={!savedMarking} inputMode="numeric" maxLength={12} value={marking.inn} aria-invalid={invalid(innMessage)}
            aria-describedby={describedBy('inn', true, innMessage)} onBlur={() => setInnTouched(true)} onChange={(event) => setMarking((value) => ({ ...value, inn: event.target.value.trim() }))} />
        </SetupField>
        <SetupField id="name" label={copy.launchFlow.fullName} hint={copy.setup.advertiser.nameHint} error={errorFor('name')}>
          <input id="name" required value={marking.name} aria-invalid={invalid(errorFor('name'))} aria-describedby={describedBy('name', true, errorFor('name'))}
            onChange={(event) => setMarking((value) => ({ ...value, name: event.target.value }))} />
        </SetupField>
        <SetupField id="phone" label={copy.launchFlow.phone} error={errorFor('phone')}>
          <PhoneInput id="phone" required={!savedMarking} placeholder="+7 (999) 123-45-67" value={marking.phone} aria-invalid={invalid(errorFor('phone'))} aria-describedby={describedBy('phone', false, errorFor('phone'))}
            onChange={(phone) => setMarking((value) => ({ ...value, phone }))} />
        </SetupField>
        {savedMarking && <p className="field__hint">{copy.settingsScreen.markingStored(savedMarking.inn_masked, savedMarking.phone_masked)}</p>}
        <section className="setup__block" aria-labelledby="confirm-title">
          <div className="setup__block-head"><h3 id="confirm-title">{copy.setup.advertiser.confirmTitle}</h3>
            <StatusBadge tone={confirmed ? 'success' : 'warning'}>{confirmed ? copy.setup.advertiser.confirmed : copy.setup.advertiser.confirmNeeded}</StatusBadge></div>
          <ol>{copy.launchFlow.markingSteps.map((text) => <li key={text}>{text}</li>)}</ol>
          <div className="notice"><p>{copy.launchFlow.markingOverwrite}</p></div>
          {(['direct', 'vk'] as const).filter((platform) => platform === 'direct' || vk).map((platform) => <label key={platform} className="check"><input type="checkbox" checked={marking.confirmed_in_platform[platform]} aria-invalid={invalid(errorFor('confirm'))} onChange={(event) => setMarking((value) => ({ ...value, confirmed_in_platform: { ...value.confirmed_in_platform, [platform]: event.target.checked } }))} />{platform === 'direct' ? copy.launchFlow.confirmDirect : copy.launchFlow.confirmVk}</label>)}
          {fieldErrorText('confirm')}
        </section>
      </>}
      </fieldset>
      {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
      <div className="actions setup__actions">
        {current > 1 && <Link className="button button--secondary" to={`/setup/${current - 1}`}><Icon name="back" />{copy.common.back}</Link>}
        <Button type="submit" className="setup__next" aria-describedby={current === 4 && !draft.approved ? 'approval-help' : undefined} disabled={busy || blocked || current === 3 && (reading || !siteReady) || current === 4 && !draft.approved}>{busy ? copy.launchFlow.saving : current === 5 ? copy.plan.title : copy.common.next}</Button>
      </div>
    </form></Card>
  </div>;
}

const connectedAs = ({ platform, external_login }: Account) => platform === 'vk' && /^\d+$/.test(external_login)
  ? copy.setup.accounts.vkCabinet(external_login) : copy.setup.accounts.connected(external_login);

function BudgetNotes({ hint, amount }: { hint: BudgetHint; amount: number }) {
  const { direct, vk } = hint.month_kop;
  if (amount < hint.minimum_kop) return <div className="notice" role="alert"><p>{copy.setup.business.budgetMinimum(formatMoney(hint.minimum_kop))}</p></div>;
  return <>
    {direct !== undefined && <p>{copy.setup.business.budgetDaily(formatMoney(dailyAverage(direct)), vk === undefined ? undefined : formatMoney(dailyAverage(vk)))}</p>}
    {hint.off.map((item) => <p key={item.platform}>{copy.setup.business.budgetVkOff(formatMoney(item.from_kop), formatMoney(item.min_daily_kop))}</p>)}
  </>;
}
