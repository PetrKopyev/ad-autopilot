import { useEffect, useState } from 'react';
import { Link, Navigate, useNavigate, useParams } from 'react-router';
import { Button, Card, ErrorBanner, Stepper } from '../components';
import { copy } from '../copy';
import { checkSite, connectAccount, createBusiness, disconnectAccount, getAuthConfig, getCounters, getGoals, getMe,
  rublesToKopecks, saveDescription, saveMarking, saveMetrica, validInn } from '../api/setup';
import type { Business, Counter, Description, DescriptionKey, Goal, Marking, SiteCheck } from '../api/setup';
import { useSession } from '../api/session';
import { NotFoundPage } from './PlaceholderPage';
import { MedicalNotice, SiteInstructions } from './SetupShared';
import './Setup.css';

interface Draft {
  business?: Business; counter: number; goals: number[]; site?: SiteCheck; description?: Description;
  approved: boolean; maxStep: number;
}
const storageKey = 'ad-autopilot-setup-draft';
function readDraft(): Draft {
  try { const value = sessionStorage.getItem(storageKey); if (value) return JSON.parse(value) as Draft; } catch { /* Continue without browser storage. */ }
  return { counter: 0, goals: [], approved: false, maxStep: 1 };
}
const fields: DescriptionKey[] = ['services', 'prices', 'address', 'difference'];

export function SetupPage() {
  const { step } = useParams();
  const navigate = useNavigate();
  const [draft, setDraft] = useState(readDraft);
  const [website, setWebsite] = useState(draft.business?.site_url ?? '');
  const [city, setCity] = useState(draft.business?.city ?? '');
  const [budget, setBudget] = useState(draft.business ? String(draft.business.month_budget_kop / 100) : '');
  const [average, setAverage] = useState(draft.business?.avg_check_kop ? String(draft.business.avg_check_kop / 100) : '');
  const [noSite, setNoSite] = useState(false);
  const session = useSession();
  const me = session.data;
  const [counters, setCounters] = useState<Counter[]>([]);
  const [goals, setGoals] = useState<Goal[]>([]);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [error, setError] = useState('');
  const [marking, setMarking] = useState<Marking>({ subject_type: 'ip', inn: '', name: '', phone: '', confirmed_in_platform: { direct: false, vk: false } });
  const [innTouched, setInnTouched] = useState(false);
  const innError = !validInn(marking.inn) ? copy.launchFlow.innError
    : marking.inn.length !== (marking.subject_type === 'ooo' ? 10 : 12) ? copy.launchFlow.innTypeError : '';
  const showInnError = (innTouched || !!marking.inn) && !!innError;
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
  useEffect(() => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(draft)); } catch { /* Keep the current form usable. */ }
  }, [draft]);
  useEffect(() => {
    const controller = new AbortController();
    if (direct) getCounters(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setCounters(result.items);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : copy.errors.generic); });
    return () => controller.abort();
  }, [direct, revision]);
  useEffect(() => {
    if (current !== 2 || !direct || counters.length !== 1) return;
    setDraft((value) => value.counter === counters[0].id ? value
      : { ...value, counter: counters[0].id, goals: [], site: undefined, description: undefined, approved: false, maxStep: 2 });
  }, [counters, current, direct]);
  useEffect(() => {
    const controller = new AbortController(); setGoals([]);
    if (draft.counter) getGoals(draft.counter, controller.signal).then((value) => {
      if (!controller.signal.aborted) setGoals(value.items);
    }).catch(() => { if (!controller.signal.aborted) setError(copy.errors.network); });
    return () => controller.abort();
  }, [draft.counter, revision]);
  useEffect(() => {
    if (current !== 3 || !draft.business) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      setReading(true);
      try {
        const site = await checkSite(controller.signal);
        if (controller.signal.aborted) return;
        setDraft((value) => ({ ...value, site, description: value.description ?? (site.status === 'ready' ? Object.fromEntries(fields.map((key) => [key,
          { ...site.description[key], value: site.description[key].verified && site.description[key].quote ? site.description[key].value : '' },
        ])) as Description : undefined) }));
        if (site.status === 'reading') timer = setTimeout(() => void poll(), 1500);
        else setReading(false);
      } catch (reason) { if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : copy.errors.generic); setReading(false); } }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [current, draft.business, revision]);
  async function act(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError('');
    try { await action(); } catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
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
      session.reload();
      setDraft({ business, counter: 0, goals: [], approved: false, maxStep: 2 }); navigate('/setup/2');
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
      setInnTouched(true);
      if (!validInn(marking.inn)) throw new Error(copy.launchFlow.innError);
      if ((marking.subject_type === 'ooo' ? 10 : 12) !== marking.inn.length) throw new Error(copy.launchFlow.innTypeError);
      if (!marking.name.trim() || !/^\+?[\d ()-]{10,25}$/.test(marking.phone) || marking.phone.replace(/\D/g, '').length < 10
        || !marking.confirmed_in_platform.direct || (vk && !marking.confirmed_in_platform.vk)) throw new Error(copy.launchFlow.markingError);
      await saveMarking(marking); navigate('/plan');
    }
  }
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
          <p id="budget-hint" className="muted">{copy.setup.business.budgetHint}</p>
          <label className="field">{copy.setup.business.visitPrice}<input inputMode="decimal" value={average} aria-describedby="average-hint" onChange={(event) => setAverage(event.target.value)} /></label>
          <p id="average-hint" className="muted">{copy.setup.business.visitPriceHint}</p>
        </>}
      </>}
      {current === 2 && <>
        <div className="account-grid">{(['direct', 'vk'] as const).map((platform) => {
          const account = me?.accounts.find((item) => item.platform === platform);
          return <Card key={platform} className="page-stack">
            <h2>{copy.setup.accounts[platform]}</h2><p className="muted">{platform === 'direct' ? copy.setup.accounts.required : copy.setup.accounts.optional}</p>
            <p>{!account || account.status === 'revoked' ? copy.setup.accounts.disconnected : account.status === 'ok' ? copy.setup.accounts.connected(account.external_login) : copy.setup.accounts.expired}</p>
            {platform === 'direct' ? account?.status === 'ok' ? <p role="status">{copy.setup.accounts.directConnected}</p>
              : <Button onClick={() => void act(async () => { await connectAccount('direct', draft.business?.id); session.reload(); })}>{copy.setup.accounts.connectDirect}</Button>
              : <Link className="button button--secondary" to="/connect/vk">{copy.launchFlow.vkConnect}</Link>}
            {account && account.status !== 'revoked' && <Button variant="secondary" onClick={() => void act(async () => { await disconnectAccount(account.id); setDraft((value) => ({ ...value, ...(platform === 'direct' ? { counter: 0, goals: [] } : {}), maxStep: 2 })); session.reload(); })}>{copy.launchFlow.disconnect}</Button>}
            <details><summary>{copy.setup.accounts.create}</summary><ol>{(platform === 'direct' ? copy.launchFlow.createDirect : copy.launchFlow.createVk).map((text) => <li key={text}>{text}</li>)}</ol></details>
          </Card>;
        })}</div>
        <Button variant="secondary" onClick={() => { session.reload(); setRevision((value) => value + 1); }}>{copy.launchFlow.refresh}</Button>
        <label className="field">{copy.setup.accounts.counter}<select disabled={!direct} required value={draft.counter || ''} onChange={(event) => setDraft((value) => ({ ...value, counter: Number(event.target.value), goals: [], site: undefined, description: undefined, approved: false, maxStep: 2 }))}>
          {!counters.length && <option value="">{copy.common.noData}</option>}{counters.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.id})</option>)}
        </select></label>
        <fieldset className="page-stack"><legend>{copy.setup.accounts.goal}</legend>{goals.map((goal) => <label className="check" key={goal.id}><input type="checkbox" checked={draft.goals.includes(goal.id)} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 2, goals: event.target.checked ? [...value.goals, goal.id] : value.goals.filter((id) => id !== goal.id) }))} />{goal.name}</label>)}</fieldset>
        <details><summary>{copy.setup.accounts.createGoal}</summary><ol>{copy.launchFlow.goalSteps.map((text) => <li key={text}>{text}</li>)}</ol></details>
        {direct && !counters.length && <div className="notice"><p>{copy.setup.website.blocked}</p><SiteInstructions /></div>}
      </>}
      {current === 3 && <div className="page-stack" role="status" aria-live="polite">
        {reading && <p>{copy.setup.website.reading}</p>}
        {draft.site && <><p>{copy.launchFlow.pagesRead(draft.site.pages_read)}</p><p>{copy.setup.website.foundServices(draft.site.services_found)}</p><p>{draft.site.counter_id ? copy.setup.website.counterFound : copy.setup.website.counterMissing}</p></>}
        {!reading && !siteReady && !blocked && <div className="notice"><p>{draft.site?.counter_id ? copy.launchFlow.counterMismatch : copy.setup.website.blocked}</p><SiteInstructions /></div>}
        <Button variant="secondary" disabled={reading} onClick={() => { setError(''); setRevision((value) => value + 1); }}>{copy.common.retry}</Button>
      </div>}
      {blocked && <div className="notice"><h2>{copy.launchFlow.blockedTitle}</h2><p>{copy.setup.website.blocked}</p>{noSite && <p>{copy.launchFlow.createSite}</p>}<SiteInstructions /></div>}
      {current === 4 && draft.description && <>
        {draft.site?.medical_level !== 'none' && <MedicalNotice />}
        {!fields.some((key) => draft.description![key].verified) && <><h2>{copy.launchFlow.ownDescription}</h2><p>{copy.launchFlow.ownDescriptionHint}</p></>}
        {fields.map((key) => { const field = draft.description![key]; return <div className="field" key={key}>
          <label className="field">{copy.setup.draft[key]}<textarea required={key === 'services'} value={field.value} aria-describedby={field.verified && field.quote?.trim() === field.value.trim() ? undefined : `source-${key}`} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: false, description: { ...value.description!, [key]: { ...field, value: event.target.value } } }))} /></label>
          {!(field.verified && field.quote?.trim() === field.value.trim()) && <p className="source" id={`source-${key}`}>{field.verified && field.quote ? copy.setup.draft.source(field.quote) : `${copy.setup.draft.missing}. ${field.question || copy.setup.draft.question}`}</p>}
        </div>; })}
        <label className="check"><input type="checkbox" checked={draft.approved} onChange={(event) => setDraft((value) => ({ ...value, maxStep: 4, approved: event.target.checked }))} />{copy.launchFlow.approved}</label>
      </>}
      {current === 5 && <>
        <p>{copy.setup.advertiser.explanation}</p>
        <label className="field">{copy.setup.advertiser.type}<select value={marking.subject_type} onChange={(event) => setMarking((value) => ({ ...value, subject_type: event.target.value as Marking['subject_type'] }))}>{Object.entries(copy.launchFlow.subjectTypes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label className="field">{copy.setup.advertiser.taxId}<input required inputMode="numeric" maxLength={12} value={marking.inn} aria-invalid={showInnError} aria-describedby={showInnError ? 'inn-help' : undefined} onBlur={() => setInnTouched(true)} onChange={(event) => setMarking((value) => ({ ...value, inn: event.target.value.trim() }))} /></label>
        {showInnError && <p className="muted" id="inn-help">{innError}</p>}
        <label className="field">{copy.launchFlow.fullName}<input required value={marking.name} onChange={(event) => setMarking((value) => ({ ...value, name: event.target.value }))} /></label>
        <label className="field">{copy.launchFlow.phone}<input type="tel" required value={marking.phone} onChange={(event) => setMarking((value) => ({ ...value, phone: event.target.value }))} /></label>
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
