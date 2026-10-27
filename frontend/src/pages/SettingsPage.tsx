import { useCallback, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { formatMoney } from '../components/Money';
import { copy } from '../copy';
import { getAutopilotStatus } from '../api/screens';
import { connectAccount, disconnectAccount, rublesToKopecks, saveDescription, saveMarking, validInn } from '../api/setup';
import type { Description, DescriptionKey, Marking, StopResult } from '../api/setup';
import { useSession } from '../api/session';
import { deleteMyData, getBusiness, maskMarking, pauseAutopilot, resumeAutopilot, saveBudget, saveBusinessSettings } from '../api/settings';
import type { BusinessSettings, StoredMarking } from '../api/settings';
import { useResource } from '../api/useResource';
import { StopNotice } from './SetupShared';
import './Setup.css';

const labels = copy.settingsScreen;
const descriptionKeys: DescriptionKey[] = ['services', 'prices', 'address', 'difference'];
const emptyMarking: Marking = { subject_type: 'ip', inn: '', name: '', phone: '', confirmed_in_platform: { direct: false, vk: false } };
const markingForm = (stored: StoredMarking | null): Marking => stored
  ? { subject_type: stored.subject_type, inn: '', name: stored.name, phone: '', confirmed_in_platform: { ...emptyMarking.confirmed_in_platform, ...stored.confirmed_in_platform } }
  : emptyMarking;
const rublesText = (kopecks: number | null) => kopecks === null ? '' : String(kopecks / 100).replace('.', ',');

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
function Block({ title, children }: { title: string; children: ReactNode }) {
  return <Card className="page-stack"><h2>{title}</h2>{children}</Card>;
}

function DescriptionBlock({ business }: { business: BusinessSettings }) {
  const section = useSection();
  const [values, setValues] = useState<Record<DescriptionKey, string>>(() => Object.fromEntries(
    descriptionKeys.map((key) => [key, business.description?.fields[key].value ?? ''])) as Record<DescriptionKey, string>);
  function submit() {
    const description = Object.fromEntries(descriptionKeys.map((key) => [key,
      { ...(business.description?.fields[key] ?? { quote: null, question: '' }), value: values[key].trim(), verified: true }])) as Description;
    void section.run(() => saveDescription(description), labels.saved);
  }
  return <Block title={copy.settings.business}>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      {descriptionKeys.map((key) => <label key={key} className="field">{labels.description[key]}
        <textarea value={values[key]} onChange={(event) => setValues((value) => ({ ...value, [key]: event.target.value }))} /></label>)}
      <Status section={section} /><SaveButton section={section} />
    </form>
  </Block>;
}

function BudgetBlock({ business }: { business: BusinessSettings }) {
  const section = useSection();
  const [budget, setBudget] = useState(rublesText(business.month_budget_kop));
  const [average, setAverage] = useState(rublesText(business.avg_check_kop));
  const [autoContinue, setAutoContinue] = useState(business.auto_continue);
  const [savedBudget, setSavedBudget] = useState(business.month_budget_kop);
  const budgetKop = rublesToKopecks(budget.trim());
  const averageKop = average.trim() === '' ? null : rublesToKopecks(average.trim());
  const averageBad = average.trim() !== '' && averageKop === null;
  function submit() {
    if (budgetKop === null || averageBad) return;
    // Неизменённый бюджет не отправляем: каждое сохранение бюджета — запись в истории и пересчёт лимитов.
    void section.run(async () => {
      if (budgetKop !== savedBudget) { await saveBudget(budgetKop); setSavedBudget(budgetKop); }
      await saveBusinessSettings(autoContinue, averageKop);
    }, labels.saved);
  }
  return <Block title={labels.budgetTitle}>
    <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <label className="field">{labels.budgetLabel}
        <input required inputMode="decimal" value={budget} aria-invalid={budgetKop === null} aria-describedby="budget-help" onChange={(event) => setBudget(event.target.value)} /></label>
      <p className="muted" id="budget-help">{budgetKop === null ? labels.budgetError : `${formatMoney(budgetKop)}. ${copy.settings.budgetHint}`}</p>
      <label className="check"><input type="checkbox" checked={autoContinue} aria-describedby="continue-help" onChange={(event) => setAutoContinue(event.target.checked)} />{copy.settings.continueNextMonth}</label>
      <p className="muted" id="continue-help">{labels.nextMonth}</p>
      <label className="field">{labels.averageLabel}
        <input inputMode="decimal" value={average} aria-invalid={averageBad} aria-describedby="average-help" onChange={(event) => setAverage(event.target.value)} /></label>
      <p className="muted" id="average-help">{averageBad ? labels.averageError : labels.averageHint}</p>
      <Status section={section} />
      <div className="actions"><Button type="submit" disabled={section.busy || budgetKop === null || averageBad}>{section.busy ? labels.saving : labels.save}</Button></div>
    </form>
  </Block>;
}

function AccountsBlock() {
  const section = useSection();
  const session = useSession();
  const [stopResult, setStopResult] = useState<StopResult>();
  const me = session.data;
  const platforms = [{ id: 'direct' as const, name: copy.setup.accounts.direct }, { id: 'vk' as const, name: copy.setup.accounts.vk }];
  return <Block title={copy.settings.accounts}>
    <p className="muted">{labels.accountsHint}</p>
    {session.error && <ErrorBanner message={session.error} onRetry={session.reload} />}
    <Status section={section} />
    {stopResult && <StopNotice result={stopResult} />}
    {me && <div className="account-grid">{platforms.map((platform) => {
      const account = me.accounts.find((item) => item.platform === platform.id);
      return <Card key={platform.id} className="page-stack"><h3>{platform.name}</h3>
        <p>{!account || account.status === 'revoked' ? copy.setup.accounts.disconnected
          : account.status === 'ok' ? copy.setup.accounts.connected(account.external_login) : copy.setup.accounts.expired}</p>
        <div className="actions">
          <Button variant="secondary" disabled={section.busy} onClick={() => { setStopResult(undefined); void section.run(async () => { await connectAccount(platform.id, account?.business_id); session.reload(); }); }}>{!account || account.status === 'revoked' ? copy.settings.connect : copy.settings.reconnect}</Button>
          {account && account.status !== 'revoked' && <Button variant="secondary" disabled={section.busy}
            onClick={() => { setStopResult(undefined); void section.run(async () => { setStopResult(await disconnectAccount(account.id)); session.reload(); }); }}>{copy.settings.disconnect}</Button>}
        </div></Card>;
    })}</div>}
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
  const innError = validInn(marking.inn) ? copy.launchFlow.innTypeError : copy.launchFlow.innError;
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
  return <Block title={copy.settings.marking}>
    <p className="muted">{labels.markingHint}</p>
    {editing ? <form className="page-stack" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <label className="field">{copy.setup.advertiser.type}<select value={marking.subject_type} onChange={(event) => set({ subject_type: event.target.value as Marking['subject_type'] })}>
        {Object.entries(copy.launchFlow.subjectTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="field">{copy.setup.advertiser.taxId}<input required inputMode="numeric" maxLength={12} value={marking.inn} aria-invalid={showInnError}
        aria-describedby={showInnError ? 'settings-inn-help' : undefined} onBlur={() => setInnTouched(true)} onChange={(event) => set({ inn: event.target.value.trim() })} /></label>
      {showInnError && <p className="muted" id="settings-inn-help">{innError}</p>}
      <label className="field">{copy.launchFlow.fullName}<input required value={marking.name} onChange={(event) => set({ name: event.target.value })} /></label>
      <label className="field">{copy.launchFlow.phone}<input type="tel" required value={marking.phone} onChange={(event) => set({ phone: event.target.value })} /></label>
      <p className="muted">{copy.launchFlow.markingOverwrite}</p>
      {(['direct', 'vk'] as const).map((platform) => <label key={platform} className="check"><input type="checkbox" checked={marking.confirmed_in_platform[platform]}
        onChange={(event) => set({ confirmed_in_platform: { ...marking.confirmed_in_platform, [platform]: event.target.checked } })} />
        {platform === 'direct' ? copy.launchFlow.confirmDirect : copy.launchFlow.confirmVk}</label>)}
      <Status section={section} />
      <div className="actions">
        <Button type="submit" disabled={section.busy}>{section.busy ? labels.saving : labels.save}</Button>
        {stored && <Button variant="secondary" disabled={section.busy} onClick={() => { section.clear(); setEditing(false); }}>{labels.markingCancel}</Button>}
      </div>
    </form> : stored && <>
      <p>{labels.markingAdvertiser(copy.launchFlow.subjectTypes[stored.subject_type], stored.name)}</p>
      <p>{labels.markingStored(stored.inn_masked, stored.phone_masked)}</p>
      <p className="muted">{confirmed.length ? labels.markingConfirmed(confirmed.map((platform) => copy.setup.accounts[platform]).join(', ')) : labels.markingNotConfirmed}</p>
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
  return <Block title={labels.autopilot}>
    {status.error && <ErrorBanner message={status.error} onRetry={status.reload} />}
    {state && <p>{state === 'draft' ? copy.screens.draft : state === 'budget_stop' ? (status.data?.auto_continue ? copy.home.exhausted : copy.screens.exhaustedPaused) : paused ? labels.autopilotPaused : labels.autopilotRunning}</p>}
    <Status section={section} />
    {state && state !== 'draft' && <div className="actions"><Button variant={paused ? 'primary' : 'secondary'} disabled={section.busy}
      onClick={() => void section.run(async () => { await (paused ? resumeAutopilot() : pauseAutopilot()); status.reload(); })}>
      {paused ? copy.settings.resume : labels.pause}</Button></div>}
  </Block>;
}

function DeleteBlock() {
  const section = useSection();
  const session = useSession();
  const navigate = useNavigate();
  const [word, setWord] = useState('');
  const confirmed = word.trim().toLowerCase() === labels.deleteWord;
  return <Block title={labels.deleteTitle}>
    <p>{labels.deleteHint}</p>
    <form className="page-stack" onSubmit={(event) => {
      event.preventDefault();
      // Итог остановки рекламы показываем на странице входа: после удаления владелец попадает туда.
      if (confirmed) void section.run(async () => { const stopResult = await deleteMyData(); session.reload(); navigate('/login', { state: { stopResult } }); });
    }}>
      <label className="field">{labels.deleteLabel}<input autoComplete="off" value={word} onChange={(event) => setWord(event.target.value)} /></label>
      <Status section={section} />
      <div className="actions"><Button type="submit" variant="danger" disabled={!confirmed || section.busy}>{labels.deleteButton}</Button></div>
    </form>
  </Block>;
}

export function SettingsPage() {
  const business = useResource(useCallback((signal: AbortSignal) => getBusiness(signal), []));
  return <div className="page-stack launch-flow">
    <h1>{copy.settings.title}</h1>
    <p className="muted">{labels.intro}</p>
    {business.loading && <p role="status">{copy.screens.loading}</p>}
    {business.error && <ErrorBanner message={business.error} onRetry={business.reload} />}
    {business.data && <>
      <BudgetBlock business={business.data} /><DescriptionBlock business={business.data} />
      <AccountsBlock /><MarkingBlock business={business.data} />
      <Block title={copy.settings.license}><p>{labels.licenseText}</p></Block>
      <AutopilotBlock /><DeleteBlock />
    </>}
  </div>;
}
