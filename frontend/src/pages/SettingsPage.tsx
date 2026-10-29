import { useCallback, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { Button, ErrorBanner, Field, Icon, PageHeader, PanelCard, RublesInput, SegmentedControl, StatusBadge, describedBy } from '../components';
import { formatMoney } from '../components/Money';
import { copy } from '../copy';
import { getAutopilotStatus } from '../api/screens';
import { connectAccount, disconnectAccount, maxBudgetRubles, rublesToKopecks, saveDescription, saveMarking, validInn } from '../api/setup';
import type { Description, DescriptionKey, Marking, StopResult } from '../api/setup';
import { useSession } from '../api/session';
import { deleteMyData, getBusiness, maskMarking, pauseAutopilot, resumeAutopilot, saveBudget, saveBusinessSettings } from '../api/settings';
import type { BusinessSettings, StoredMarking } from '../api/settings';
import { useResource } from '../api/useResource';
import { StopNotice } from './SetupShared';
import './Settings.css';

const labels = copy.settingsScreen;
const descriptionKeys: DescriptionKey[] = ['services', 'prices', 'address', 'difference'];
const emptyMarking: Marking = { subject_type: 'ip', inn: '', name: '', phone: '', confirmed_in_platform: { direct: false, vk: false } };
const markingForm = (stored: StoredMarking | null): Marking => stored
  ? { subject_type: stored.subject_type, inn: '', name: stored.name, phone: '', confirmed_in_platform: { ...emptyMarking.confirmed_in_platform, ...stored.confirmed_in_platform } }
  : emptyMarking;
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

function DescriptionBlock({ business }: { business: BusinessSettings }) {
  const section = useSection();
  const [values, setValues] = useState<Record<DescriptionKey, string>>(() => Object.fromEntries(
    descriptionKeys.map((key) => [key, business.description?.fields[key].value ?? ''])) as Record<DescriptionKey, string>);
  function submit() {
    const description = Object.fromEntries(descriptionKeys.map((key) => [key,
      { ...(business.description?.fields[key] ?? { quote: null, question: '' }), value: key === 'prices' ? '' : values[key].trim(), verified: true }])) as Description;
    void section.run(() => saveDescription(description), labels.saved);
  }
  return <Block id="business" title={copy.settings.business} hint={labels.businessHint}>
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
    if (budgetKop === null) return;
    // Неизменённый бюджет не отправляем: каждое сохранение бюджета — запись в истории и пересчёт лимитов.
    void section.run(async () => {
      if (budgetKop !== savedBudget) { setWarning(await saveBudget(budgetKop)); setSavedBudget(budgetKop); }
      await saveBusinessSettings(autoContinue, averageKop);
    }, labels.saved);
  }
  const budgetHint = budgetKop === null ? undefined : `${formatMoney(budgetKop)}. ${copy.settings.budgetHint}`;
  const budgetError = budgetKop === null ? labels.budgetError : undefined;
  return <Block id="budget" title={labels.budgetTitle} hint={<><Icon name="shield" />{copy.plan.promise}</>}>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <Field id="settings-budget" label={labels.budgetLabel} hint={budgetHint} error={budgetError}>
        <RublesInput id="settings-budget" className="input mono" required max={maxBudgetRubles} value={budget} aria-invalid={budgetKop === null}
          aria-describedby={describedBy('settings-budget', budgetHint, budgetError)} onChange={setBudget} /></Field>
      <div className="settings-check">
        <label className="check"><input type="checkbox" checked={autoContinue} aria-describedby="continue-help" onChange={(event) => setAutoContinue(event.target.checked)} />{copy.settings.continueNextMonth}</label>
        <p className="field__hint" id="continue-help">{labels.nextMonth}</p>
      </div>
      <Field id="settings-average" label={labels.averageLabel} hint={labels.averageHint}>
        <RublesInput id="settings-average" className="input mono" max={maxBudgetRubles} value={average} aria-describedby={describedBy('settings-average', labels.averageHint)} onChange={setAverage} /></Field>
      <Status section={section} />
      {warning && <div className="notice" role="status"><p>{warning}</p></div>}
      <div className="actions"><Button type="submit" disabled={section.busy || budgetKop === null}>{section.busy ? labels.saving : labels.save}</Button></div>
    </form>
  </Block>;
}

function AccountsBlock() {
  const section = useSection();
  const session = useSession();
  const [stopResult, setStopResult] = useState<StopResult>();
  const me = session.data;
  const platforms = [{ id: 'direct' as const, name: copy.setup.accounts.direct }, { id: 'vk' as const, name: copy.setup.accounts.vk }];
  return <Block id="accounts" title={copy.settings.accounts} hint={labels.accountsHint}>
    {session.error && <ErrorBanner message={session.error} onRetry={session.reload} />}
    <Status section={section} />
    {stopResult && <StopNotice result={stopResult} />}
    {me && <ul className="settings-accounts">{platforms.map((platform) => {
      const account = me.accounts.find((item) => item.platform === platform.id);
      const off = !account || account.status === 'revoked';
      return <li key={platform.id} className="settings-account">
        <div className="settings-account__name"><h3><span className={`platform-badge__dot platform-badge--${platform.id}`} aria-hidden="true" />{platform.name}</h3>
          {off ? <StatusBadge>{copy.setup.accounts.disconnected}</StatusBadge>
            : account.status === 'ok' ? <StatusBadge tone="success">{copy.setup.accounts.connected(account.external_login)}</StatusBadge>
              : <StatusBadge tone="warning">{copy.setup.accounts.expired}</StatusBadge>}</div>
        <div className="actions">
          <Button variant="secondary" disabled={section.busy} onClick={() => { setStopResult(undefined); void section.run(async () => { await connectAccount(platform.id, account?.business_id); session.reload(); }); }}>{!account || account.status === 'revoked' ? copy.settings.connect : copy.settings.reconnect}</Button>
          {account && account.status !== 'revoked' && <Button variant="secondary" disabled={section.busy}
            onClick={() => { setStopResult(undefined); void section.run(async () => { setStopResult(await disconnectAccount(account.id)); session.reload(); }); }}>{copy.settings.disconnect}</Button>}
        </div></li>;
    })}</ul>}
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

function AutopilotBlock() {
  const section = useSection();
  const status = useResource(useCallback((signal: AbortSignal) => getAutopilotStatus(signal), []));
  const state = status.data?.status;
  const paused = state === 'paused' || state === 'budget_stop';
  const [warning, setWarning] = useState<string | null>(null);
  function toggle() {
    void section.run(async () => { if (paused) setWarning(await resumeAutopilot()); else { setWarning(null); await pauseAutopilot(); } status.reload(); });
  }
  return <Block id="autopilot" title={labels.autopilot}>
    {status.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {status.error && <ErrorBanner message={status.error} onRetry={status.reload} />}
    {state && state !== 'draft' && <SegmentedControl label={copy.autopilotMode.label} value={paused ? 'paused' : 'working'} disabled={section.busy}
      onChange={(mode) => { if ((mode === 'paused') !== paused) toggle(); }}
      options={[{ value: 'working', label: copy.autopilotMode.working }, { value: 'paused', label: copy.autopilotMode.paused }]} />}
    {state && <p className="settings-hint">{state === 'draft' ? copy.screens.draft : state === 'budget_stop' ? (status.data?.auto_continue ? copy.home.exhausted : copy.screens.exhaustedPaused) : paused ? labels.autopilotPaused : labels.autopilotRunning}</p>}
    <Status section={section} />
    {warning && <div className="notice" role="status"><p>{warning}</p></div>}
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
  return <div className="page-stack settings">
    <PageHeader title={copy.settings.title} description={labels.intro} />
    {business.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {business.error && <ErrorBanner message={business.error} onRetry={business.reload} />}
    {business.data && <div className="settings-layout">
      <div className="settings-column"><BudgetBlock business={business.data} /><DescriptionBlock business={business.data} /></div>
      <div className="settings-column">
        <AutopilotBlock /><AccountsBlock /><MarkingBlock business={business.data} />
        <Block id="license" title={copy.settings.license}><p className="settings-hint">{labels.licenseText}</p></Block>
        <DeleteBlock />
      </div>
    </div>}
  </div>;
}
