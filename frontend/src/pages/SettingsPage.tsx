import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router';
import { Button, ConfirmDialog, ErrorBanner, Field, Icon, PageHeader, PanelCard, RublesInput, SegmentedControl, StatusBadge, describedBy } from '../components';
import { formatMoney } from '../components/Money';
import { copy } from '../copy';
import { getAutopilotStatus } from '../api/screens';
import { connectAccount, disconnectAccount, getAuthConfig, getGoals, rublesToKopecks, saveDescription, saveMarking, tooMuchMoney, validInn } from '../api/setup';
import type { Description, DescriptionKey, Marking, StopResult } from '../api/setup';
import { useSession } from '../api/session';
import { deleteMyData, getBusiness, maskMarking, pauseAutopilot, resumeAutopilot, saveBudget, saveBusinessSettings } from '../api/settings';
import type { BusinessSettings, StoredMarking } from '../api/settings';
import type { AutopilotStatus } from '../api/types';
import { useResource } from '../api/useResource';
import { StopNotice } from './SetupShared';
import './Settings.css';

const labels = copy.settingsScreen;
const descriptionKeys: DescriptionKey[] = ['services', 'prices', 'address', 'difference'];
const emptyMarking: Marking = { subject_type: 'ip', inn: '', name: '', phone: '', confirmed_in_platform: { direct: false, vk: false } };
const markingForm = (stored: StoredMarking | null): Marking => stored
  ? { subject_type: stored.subject_type, inn: '', name: stored.name, phone: '', confirmed_in_platform: { ...emptyMarking.confirmed_in_platform, ...stored.confirmed_in_platform } }
  : emptyMarking;
// Набранную сумму повторяем как есть: 1 500,50 ₽ не превращается в подписи в 1 501 ₽.
const exactMoney = (kopecks: number) => kopecks % 100 ? `${(kopecks / 100).toLocaleString('ru-RU', { minimumFractionDigits: 2 })} ₽` : formatMoney(kopecks);
const rublesText = (kopecks: number | null) => kopecks === null ? '' : String(Math.round(kopecks / 100));

// One section with its own save status, so a failed save never hides the others.
function useSection() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  async function run(action: () => Promise<void>, message?: string) {
    setBusy(true); setError(undefined); setDone(undefined);
    try { await action(); setDone(message); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  return { busy, error, done, run, clear: () => { setError(undefined); setDone(undefined); } };
}
type Section = ReturnType<typeof useSection>;
function Status({ section }: { section: Section }) {
  return <>{section.error && <ErrorBanner message={section.error} onDismiss={section.clear} />}
    {section.done && <p role="status">{section.done}</p>}</>;
}
function SaveButton({ section }: { section: Section }) {
  return <div className="actions"><Button type="submit" disabled={section.busy}>{section.busy ? labels.saving : labels.save}</Button></div>;
}
function Block({ id, title, hint, danger, children }: { id: string; title: string; hint?: ReactNode; danger?: boolean; children: ReactNode }) {
  return <PanelCard title={title} titleId={`settings-${id}-title`} className={danger ? 'settings-danger' : undefined}>
    <div className="page-stack">{hint && <p className="settings-hint">{hint}</p>}{children}</div>
  </PanelCard>;
}

function DescriptionBlock({ business, launched }: { business: BusinessSettings; launched: boolean }) {
  const section = useSection();
  const [values, setValues] = useState<Record<DescriptionKey, string>>(() => Object.fromEntries(
    descriptionKeys.map((key) => [key, business.description?.fields[key].value ?? ''])) as Record<DescriptionKey, string>);
  function submit() {
    const description = Object.fromEntries(descriptionKeys.map((key) => [key,
      { ...(business.description?.fields[key] ?? { quote: null, question: '' }), value: key === 'prices' ? '' : values[key].trim(), verified: true }])) as Description;
    void section.run(() => saveDescription(description), launched ? labels.savedLaunched : labels.saved);
  }
  // После запуска честно говорим, что объявления на площадках от правки не перестроятся.
  return <Block id="business" title={copy.settings.business} hint={launched ? <>{labels.businessHint} {labels.businessLaunched}</> : labels.businessHint}>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      {descriptionKeys.filter((key) => key !== 'prices').map((key) => {
        const id = `settings-${key}`;
        const hint = key === 'services' ? copy.setup.draft.servicesHint : undefined;
        return <Field key={key} id={id} label={labels.description[key]} hint={hint}>
          <textarea id={id} className="input" rows={key === 'services' ? 6 : 3} value={values[key]} aria-describedby={describedBy(id, hint)}
            onChange={(event) => setValues((value) => ({ ...value, [key]: event.target.value }))} /></Field>;
      })}
      <Status section={section} /><SaveButton section={section} />
    </form>
  </Block>;
}

function BudgetBlock({ business }: { business: BusinessSettings }) {
  const section = useSection();
  const [budget, setBudget] = useState(String(Math.round(business.month_budget_kop / 100)));
  const [average, setAverage] = useState(rublesText(business.avg_check_kop));
  const [autoContinue, setAutoContinue] = useState(business.auto_continue);
  const [savedBudget, setSavedBudget] = useState(business.month_budget_kop);
  const [warning, setWarning] = useState<string | null>(null);
  const budgetKop = budget === String(Math.round(savedBudget / 100)) ? savedBudget : rublesToKopecks(budget);
  const averageKop = average === '' ? null : average === rublesText(business.avg_check_kop) ? business.avg_check_kop : rublesToKopecks(average);
  function submit() {
    if (budgetKop === null || budgetError || averageError) return;
    // Неизменённый бюджет не отправляем: каждое сохранение бюджета — запись в истории и пересчёт лимитов.
    void section.run(async () => {
      if (budgetKop !== savedBudget) { setWarning(await saveBudget(budgetKop)); setSavedBudget(budgetKop); }
      await saveBusinessSettings(autoContinue, averageKop);
    }, labels.saved);
  }
  // До сохранения подпись говорит, что будет после него: сумма ещё не действует.
  const budgetHint = budgetKop === null || tooMuchMoney(budgetKop) ? undefined
    : budgetKop === savedBudget ? undefined : labels.budgetPending(exactMoney(budgetKop));
  const budgetError = budgetKop === null ? labels.budgetError : tooMuchMoney(budgetKop) ? copy.launchFlow.moneyTooBig : undefined;
  const averageError = average !== '' && (averageKop === null || tooMuchMoney(averageKop)) ? (averageKop === null ? copy.launchFlow.moneyError : copy.launchFlow.moneyTooBig) : undefined;
  return <Block id="budget" title={labels.budgetTitle} hint={<><Icon name="shield" />{copy.plan.promise}</>}>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <Field id="settings-budget" label={labels.budgetLabel} hint={budgetHint} error={budgetError}>
        <RublesInput id="settings-budget" className="input mono" required value={budget} aria-invalid={!!budgetError}
          aria-describedby={describedBy('settings-budget', budgetHint, budgetError)} onChange={setBudget} /></Field>
      <div className="settings-check">
        <label className="check"><input type="checkbox" checked={autoContinue} aria-describedby="continue-help" onChange={(event) => setAutoContinue(event.target.checked)} />{copy.settings.continueNextMonth}</label>
        <p className="field__hint" id="continue-help">{labels.nextMonth}</p>
      </div>
      <Field id="settings-average" label={labels.averageLabel} hint={labels.averageHint} error={averageError}>
        <RublesInput id="settings-average" className="input mono" value={average} aria-invalid={!!averageError}
          aria-describedby={describedBy('settings-average', labels.averageHint, averageError)} onChange={setAverage} /></Field>
      <Status section={section} />
      {warning && <div className="notice" role="status"><p>{warning}</p></div>}
      <div className="actions"><Button type="submit" disabled={section.busy || !!budgetError || !!averageError}>{section.busy ? labels.saving : labels.save}</Button></div>
    </form>
  </Block>;
}

type StatusResource = ReturnType<typeof useResource<AutopilotStatus>>;

/** Счётчик и цели выбирают в мастере: до запуска туда ведёт «Изменить», после запуска они не меняются. */
function MetricaRow({ business, draft }: { business: BusinessSettings; draft: boolean }) {
  const counter = business.metrica_counter_id;
  const goals = useResource(useCallback((signal: AbortSignal) => counter ? getGoals(counter, signal) : Promise.resolve({ items: [] }), [counter]));
  // Цели Метрики не загрузились — называем их числом, а не прячем строку.
  const names = business.lead_goal_ids.map((id) => goals.data?.items.find((goal) => goal.id === id)?.name).filter(Boolean);
  const goalText = names.length === business.lead_goal_ids.length ? names.map((name) => `«${name}»`).join(', ') : labels.metricaGoals(business.lead_goal_ids.length);
  return <li className="settings-account">
    <div className="settings-account__name"><h3><span className="platform-badge__dot platform-badge--metrica" aria-hidden="true" />{labels.metrica}</h3>
      <p className="settings-account__line">{counter && business.lead_goal_ids.length ? labels.metricaLine(String(counter), goalText) : labels.metricaNone}</p>
      {!draft && <p className="field__hint">{labels.metricaLocked}</p>}</div>
    {draft && <div className="actions"><Link className="button button--secondary" to="/setup/2">{labels.metricaEdit}</Link></div>}
  </li>;
}

function AccountsBlock({ business, status }: { business: BusinessSettings; status: StatusResource }) {
  const section = useSection();
  const session = useSession();
  const [stopResult, setStopResult] = useState<StopResult>();
  const [leaving, setLeaving] = useState<{ id: number; name: string }>();
  const [demo, setDemo] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getAuthConfig(controller.signal).then((config) => { if (!controller.signal.aborted) setDemo(config.demo); }).catch(() => {});
    return () => controller.abort();
  }, []);
  const me = session.data;
  const platforms = [{ id: 'direct' as const, name: copy.setup.accounts.direct }, { id: 'vk' as const, name: copy.setup.accounts.vk }];
  function disconnect(id: number) {
    void section.run(async () => { setStopResult(await disconnectAccount(id)); setLeaving(undefined); session.reload(); status.reload(); });
  }
  return <Block id="accounts" title={copy.settings.accounts} hint={labels.accountsHint}>
    {session.error && <ErrorBanner message={session.error} onRetry={session.reload} />}
    {!leaving && <Status section={section} />}
    {stopResult && <StopNotice result={stopResult} />}
    {me && <ul className="settings-accounts">{platforms.map((platform) => {
      const account = me.accounts.find((item) => item.platform === platform.id);
      const off = !account || account.status === 'revoked';
      const connectLabel = off ? copy.settings.connect : copy.settings.reconnect;
      return <li key={platform.id} className="settings-account">
        <div className="settings-account__name"><h3><span className={`platform-badge__dot platform-badge--${platform.id}`} aria-hidden="true" />{platform.name}</h3>
          {off ? <StatusBadge>{copy.setup.accounts.disconnected}</StatusBadge>
            : account.status === 'ok' ? <StatusBadge tone="success">{copy.setup.accounts.connected(account.external_login)}</StatusBadge>
              : <StatusBadge tone="warning">{copy.setup.accounts.expired}</StatusBadge>}</div>
        <div className="actions">
          {/* ВКонтакте подключают ключом или входом на отдельной странице — как в мастере. */}
          {platform.id === 'vk' && !demo ? <Link className="button button--secondary" to="/connect/vk" state={{ back: '/settings' }}>{connectLabel}</Link>
            : (off || !demo) && <Button variant="secondary" disabled={section.busy} onClick={() => { setStopResult(undefined); void section.run(async () => { await connectAccount(platform.id, account?.business_id); session.reload(); status.reload(); }); }}>{connectLabel}</Button>}
          {account && !off && <Button variant="secondary" disabled={section.busy}
            onClick={() => { setStopResult(undefined); section.clear(); setLeaving({ id: account.id, name: platform.name }); }}>{copy.settings.disconnect}</Button>}
        </div></li>;
    })}
      {status.data && <MetricaRow business={business} draft={status.data.status === 'draft'} />}</ul>}
    <ConfirmDialog open={!!leaving} title={copy.settings.disconnectTitle(leaving?.name ?? '')} confirm={copy.settings.disconnectYes} confirming={copy.settings.disconnecting}
      cancel={copy.screens.cancel} busy={section.busy} error={leaving && section.error} onConfirm={() => leaving && disconnect(leaving.id)}
      onClose={() => { section.clear(); setLeaving(undefined); }}>
      <ul>{copy.settings.disconnectSteps.map((text) => <li key={text}>{text}</li>)}</ul>
      {demo && <p><strong>{copy.settings.disconnectDemo}</strong></p>}
    </ConfirmDialog>
  </Block>;
}

function MarkingBlock({ business }: { business: BusinessSettings }) {
  const section = useSection();
  const [stored, setStored] = useState<StoredMarking | null>(business.marking);
  // Сохранённые ИНН и телефон сервер целиком не отдаёт: форма открывается по «Изменить», и их вводят заново.
  const [editing, setEditing] = useState(!business.marking);
  const [marking, setMarking] = useState<Marking>(() => markingForm(stored));
  const [innTouched, setInnTouched] = useState(false);
  const set = (patch: Partial<Marking>) => setMarking((value) => ({ ...value, ...patch }));
  const innOk = validInn(marking.inn) && (marking.subject_type === 'ooo' ? 10 : 12) === marking.inn.length;
  const showInnError = (innTouched || !!marking.inn) && !innOk;
  const innError = copy.launchFlow.innError(marking.subject_type);
  const confirmed = stored ? (['direct', 'vk'] as const).filter((platform) => stored.confirmed_in_platform[platform]) : [];
  function edit() {
    section.clear(); setMarking(markingForm(stored)); setInnTouched(false); setEditing(true);
  }
  function submit() {
    setInnTouched(true);
    if (!innOk) { void section.run(() => Promise.reject(new Error(innError))); return; }
    if (!marking.name.trim() || marking.phone.replace(/\D/g, '').length < 10) {
      void section.run(() => Promise.reject(new Error(copy.launchFlow.markingError))); return;
    }
    void section.run(async () => { await saveMarking(marking); setStored(maskMarking(marking)); setEditing(false); }, labels.markingSaved);
  }
  return <Block id="marking" title={copy.settings.marking} hint={labels.markingHint}>
    {editing ? <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <div className="settings-pair">
        <Field id="settings-subject" label={copy.setup.advertiser.type}><select id="settings-subject" className="input" value={marking.subject_type} onChange={(event) => set({ subject_type: event.target.value as Marking['subject_type'] })}>
          {Object.entries(copy.launchFlow.subjectTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
        <Field id="settings-inn" label={copy.setup.advertiser.taxId} error={showInnError ? innError : undefined}>
          <input id="settings-inn" className="input mono" required inputMode="numeric" maxLength={12} value={marking.inn} aria-invalid={showInnError}
            aria-describedby={describedBy('settings-inn', undefined, showInnError)} onBlur={() => setInnTouched(true)} onChange={(event) => set({ inn: event.target.value.trim() })} /></Field>
      </div>
      <Field id="settings-name" label={copy.launchFlow.fullName}><input id="settings-name" className="input" required value={marking.name} onChange={(event) => set({ name: event.target.value })} /></Field>
      <Field id="settings-phone" label={copy.launchFlow.phone}><input id="settings-phone" className="input" type="tel" required value={marking.phone} onChange={(event) => set({ phone: event.target.value })} /></Field>
      <p className="notice">{copy.launchFlow.markingOverwrite}</p>
      {(['direct', 'vk'] as const).map((platform) => <label key={platform} className="check"><input type="checkbox" checked={marking.confirmed_in_platform[platform]}
        onChange={(event) => set({ confirmed_in_platform: { ...marking.confirmed_in_platform, [platform]: event.target.checked } })} />
        {platform === 'direct' ? copy.launchFlow.confirmDirect : copy.launchFlow.confirmVk}</label>)}
      <Status section={section} />
      <div className="actions">
        <Button type="submit" disabled={section.busy}>{section.busy ? labels.saving : labels.save}</Button>
        {stored && <Button variant="secondary" disabled={section.busy} onClick={() => { section.clear(); setEditing(false); }}>{labels.markingCancel}</Button>}
      </div>
    </form> : stored && <>
      <div className="settings-stored">
        <p className="settings-stored__name">{labels.markingAdvertiser(copy.launchFlow.subjectTypes[stored.subject_type], stored.name)}</p>
        <p>{labels.markingStored(stored.inn_masked, stored.phone_masked)}</p>
      </div>
      <StatusBadge tone={confirmed.length ? 'success' : 'warning'}>{confirmed.length ? labels.markingConfirmed(confirmed.map((platform) => copy.setup.accounts[platform]).join(', ')) : labels.markingNotConfirmed}</StatusBadge>
      <Status section={section} />
      <div className="actions"><Button variant="secondary" onClick={edit}>{labels.markingEdit}</Button></div>
    </>}
  </Block>;
}

function AutopilotBlock({ status }: { status: StatusResource }) {
  const section = useSection();
  const state = status.data?.status;
  const paused = state === 'paused' || state === 'budget_stop';
  const [warning, setWarning] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  // «Пауза» здесь — то же, что на главной: остановка всей рекламы, с тем же подтверждением.
  function resume() { void section.run(async () => { setWarning(await resumeAutopilot()); status.reload(); }); }
  function pause() {
    void section.run(async () => { setWarning(null); await pauseAutopilot(); setAsking(false); status.reload(); });
  }
  return <Block id="autopilot" title={labels.autopilot}>
    {status.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {status.error && <ErrorBanner message={status.error} onRetry={status.reload} />}
    {state && state !== 'draft' && <SegmentedControl label={copy.autopilotMode.label} value={paused ? 'paused' : 'working'} disabled={section.busy}
      onChange={(mode) => { if ((mode === 'paused') === paused) return; if (paused) resume(); else { section.clear(); setAsking(true); } }}
      options={[{ value: 'working', label: copy.autopilotMode.working }, { value: 'paused', label: copy.autopilotMode.paused }]} />}
    {state && <p className="settings-hint">{state === 'draft' ? copy.screens.draft : state === 'budget_stop' ? (status.data?.auto_continue ? copy.home.exhausted : copy.screens.exhaustedPaused) : paused ? labels.autopilotPaused : labels.autopilotRunning}</p>}
    {!asking && <Status section={section} />}
    {warning && <div className="notice" role="status"><p>{warning}</p></div>}
    <ConfirmDialog open={asking} title={copy.screens.stopTitle} confirm={copy.screens.confirmStop} confirming={copy.screens.stopping} cancel={copy.screens.cancel}
      busy={section.busy} error={asking ? section.error : undefined} onConfirm={pause} onClose={() => { section.clear(); setAsking(false); }}><p>{copy.screens.stopDescription}</p></ConfirmDialog>
  </Block>;
}

function DeleteBlock() {
  const section = useSection();
  const navigate = useNavigate();
  const [word, setWord] = useState('');
  const confirmed = word.trim().toLowerCase() === labels.deleteWord;
  return <Block id="delete" title={labels.deleteTitle} hint={labels.deleteHint} danger>
    <form className="page-stack" onSubmit={(event) => {
      event.preventDefault();
      // Итог остановки рекламы показываем на странице входа: после удаления владелец попадает туда, а сессию она
      // перечитывает сама.
      if (confirmed) void section.run(async () => { navigate('/login', { state: { stopResult: await deleteMyData() } }); });
    }}>
      <Field id="settings-delete" label={labels.deleteLabel}><input id="settings-delete" className="input" autoComplete="off" value={word} onChange={(event) => setWord(event.target.value)} /></Field>
      <Status section={section} />
      <div className="actions"><Button type="submit" variant="danger" disabled={!confirmed || section.busy}>{labels.deleteButton}</Button></div>
    </form>
  </Block>;
}

export function SettingsPage() {
  const business = useResource(useCallback((signal: AbortSignal) => getBusiness(signal), []));
  const status = useResource(useCallback((signal: AbortSignal) => getAutopilotStatus(signal), []));
  const { hash } = useLocation();
  // Ссылки «В настройках» с других экранов ведут к своему блоку: он появляется, когда загрузились настройки.
  useEffect(() => {
    if (business.data && hash) document.getElementById(decodeURIComponent(hash.slice(1)))?.closest('.panel')?.scrollIntoView({ block: 'start' });
  }, [business.data, hash]);
  return <div className="page-stack settings">
    <PageHeader title={copy.settings.title} description={labels.intro} />
    {business.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {business.error && <ErrorBanner message={business.error} onRetry={business.reload} />}
    {business.data && <div className="settings-layout">
      <div className="settings-column"><BudgetBlock business={business.data} /><DescriptionBlock business={business.data} launched={!!status.data && status.data.status !== 'draft' && status.data.status !== 'no_business'} /></div>
      <div className="settings-column">
        <AutopilotBlock status={status} /><AccountsBlock business={business.data} status={status} /><MarkingBlock business={business.data} />
        <Block id="license" title={copy.settings.license}><p className="settings-hint">{labels.licenseText}</p></Block>
        <DeleteBlock />
      </div>
    </div>}
  </div>;
}
