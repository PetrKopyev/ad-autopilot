import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, Card, ConfirmDialog, ErrorBanner, Icon, PageHeader, PanelCard, PillSteps, PlatformBadge, StatusBadge } from '../components';
import { AdPreview } from '../components/AdPreview';
import { formatMoney } from '../components/Money';
import { getForecast } from '../api/forecast';
import type { Forecast } from '../api/forecast';
import { dailyAverage } from '../api/period';
import { getPlan, launchPlan, maxDirectGroups, regenerateImages, saveAssets, uploadPhoto } from '../api/plan';
import type { Plan, PlanGroup } from '../api/plan';
import { getBusiness } from '../api/settings';
import { useResource } from '../api/useResource';
import { copy } from '../copy';
import { MedicalNotice } from './SetupShared';
import './Plan.css';

interface Job { group: number; kind: 'generate' | 'upload'; run: () => Promise<Plan> }

// Директ считает прогноз несколько секунд или минут: спрашиваем каждые 5 с, но не дольше двух минут.
const forecastDelay = 5000;
const forecastPatience = 120_000;

function selectedIds(plan: Plan) {
  return plan.groups.flatMap((group) => group.assets.filter((asset) => asset.selected).map((asset) => asset.id));
}

// Сервер отвечает всем планом; отметки, которые поменяли, пока шёл запрос, важнее отметок из ответа.
function merge(local: Plan, fresh: Plan): Plan {
  const selected = new Map(local.groups.flatMap((group) => group.assets.map((asset) => [asset.id, asset.selected] as const)));
  return { ...fresh, groups: fresh.groups.map((group) => ({ ...group, assets: group.assets.map((asset) => ({ ...asset, selected: selected.get(asset.id) ?? asset.selected })) })) };
}

const platformMonth = (plan: Plan, platform: 'direct' | 'vk') => platform === 'direct' ? plan.direct_budget_kop : plan.vk_budget_kop;

/** Прогноз приходит только с сервера; при ошибке или без эндпоинта блока нет. */
function useForecast(enabled: boolean) {
  const [state, setState] = useState<{ forecast?: Forecast; slow?: boolean }>({});
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController(); const started = Date.now(); let timer = 0;
    const load = () => void getForecast(controller.signal).then((forecast) => {
      if (controller.signal.aborted) return;
      const slow = forecast.status === 'pending' && Date.now() - started >= forecastPatience;
      setState({ forecast, slow });
      if (forecast.status === 'pending' && !slow) timer = window.setTimeout(load, forecastDelay);
    }, () => { if (!controller.signal.aborted) setState({}); });
    load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [enabled]);
  return state;
}

function ForecastPanel({ forecast, slow }: { forecast: Forecast; slow?: boolean }) {
  const ready = forecast.status === 'ready' && forecast.leads_min !== null && forecast.leads_max !== null;
  return <PanelCard title={copy.plan.forecast} titleId="plan-forecast" className="plan-forecast" action={forecast.demo && <span className="plan-demo">{copy.plan.demo}</span>}>
    {ready ? <p className="plan-forecast__value"><span className="mono">{copy.plan.forecastLeads(forecast.leads_min!, forecast.leads_max!)}</span> {copy.plan.forecastUnit(forecast.leads_max!)}</p>
      : forecast.status === 'pending' && <p className="loading" role="status">{slow ? copy.plan.forecastSlow : copy.plan.forecastPending}</p>}
    {forecast.explanation && <p>{forecast.explanation}</p>}
    {ready && !forecast.explanation_by_ai && <p className="muted">{copy.plan.forecastMayErr}</p>}
    {forecast.vk && <p className="muted">{forecast.vk.label}</p>}
    {ready && forecast.sources.length > 0 && <div className="plan-forecast__sources"><p className="plan-label">{copy.plan.forecastSources}</p>
      <ul>{forecast.sources.map((item) => <li key={item.what}>{item.what}: {item.url ? <a href={item.url} target="_blank" rel="noreferrer">{item.source}</a> : item.source}</li>)}</ul>
    </div>}
  </PanelCard>;
}

function BudgetRows({ plan }: { plan: Plan }) {
  return <>{(['direct', 'vk'] as const).map((platform) => { const month = platformMonth(plan, platform); return month > 0 && <p key={platform} className="plan-budget__row">
    {plan.daily_kop[platform] === undefined ? copy.plan.platformMonth(copy.setup.accounts[platform], formatMoney(month)) : copy.plan.platformBudget(copy.setup.accounts[platform], formatMoney(month), formatMoney(dailyAverage(month)))}
  </p>; })}</>;
}

export function PlanPage() {
  const resource = useResource(getPlan);
  const business = useResource(getBusiness);
  const [edited, setEdited] = useState<Plan>();
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [chosen, setChosen] = useState<number>();
  const [confirming, setConfirming] = useState(false);
  const [previewAd, setPreviewAd] = useState<Record<number, number>>({});
  // Сервер держит блокировку бизнеса всю генерацию: запросы картинок идут по одному, остальные ждут в очереди.
  const queue = useRef<Job[]>([]);
  const plan = edited ?? resource.data;
  const planRef = useRef(plan);
  planRef.current = plan;
  const { forecast, slow } = useForecast(plan?.status === 'draft');
  const medical = !!plan && plan.medical_level !== 'none';
  const groupReady = (group: PlanGroup) => group.assets.some((asset) => asset.selected);
  const imagesReady = !!plan?.groups.length && plan.groups.every(groupReady);
  async function act(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError('');
    try { await action(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  function update(next: Plan) {
    planRef.current = next; setEdited(next);
  }
  function enqueue(job: Job) {
    setError(''); queue.current.push(job); setJobs([...queue.current]);
    if (queue.current.length === 1) void drain();
  }
  async function drain() {
    for (let job = queue.current[0]; job; job = queue.current[0]) {
      try {
        await saveAssets(selectedIds(planRef.current!));
        const fresh = await job.run();
        update(merge(planRef.current!, fresh));
      } catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
      queue.current.shift(); setJobs([...queue.current]);
    }
  }
  // В одну колонку панель группы ниже списка: если её начала не видно, выбор группы ведёт к ней.
  function choose(id: number) {
    setChosen(id);
    requestAnimationFrame(() => {
      const panel = document.getElementById('plan-group')?.closest('.panel');
      const top = panel?.getBoundingClientRect().top ?? 0;
      if (panel && (top < 0 || top > window.innerHeight * 0.6)) {
        panel.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
      }
    });
  }
  // Отменить можно только ждущий запрос: первый в очереди уже выполняется.
  function cancel(group: number, kind: Job['kind']) {
    queue.current = queue.current.filter((job, index) => index === 0 || job.group !== group || job.kind !== kind); setJobs([...queue.current]);
  }
  function jobState(group: number, kind: Job['kind']) {
    const index = jobs.findIndex((job) => job.group === group && job.kind === kind);
    return index < 0 ? undefined : index === 0 ? 'running' : 'queued';
  }
  const generationsLeft = (plan?.generations_remaining ?? 0) - jobs.filter((job) => job.kind === 'generate').length;
  async function launch(current: Plan) {
    await saveAssets(selectedIds(current));
    try { await launchPlan(current.id, acknowledged); }
    catch (reason) {
      // Объяснение прошлой попытки уже неправда: о новой неудаче говорит сообщение об ошибке.
      setEdited({ ...current, launch_error: null }); throw reason;
    }
    setEdited({ ...current, status: 'launched' });
  }
  const launched = plan?.status === 'launched';
  const group = plan?.groups.find((item) => item.id === chosen) ?? plan?.groups[0];
  const servicesCut = !!plan && plan.direct_services_total > maxDirectGroups;
  const steps = copy.plan.steps.map((label, index) => ({ label, to: index < 5 ? `/setup/${index + 1}` : undefined }));
  return <div className="page-stack plan-page">
    <PageHeader title={launched ? copy.launchFlow.launched : copy.plan.title} description={plan && copy.plan.summary(formatMoney(plan.month_budget_kop))}
      actions={!launched && <PillSteps label={copy.plan.stepsLabel} steps={steps} current={5} />} />
    {resource.loading && <p className="loading" role="status">{copy.launchFlow.loading}</p>}
    {resource.error && <ErrorBanner message={resource.error} onRetry={resource.reload} />}
    {error && !confirming && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    {plan && <>
      {plan.off.map((item) => <div key={item.platform} className="notice" role="status">
        <p>{(launched ? copy.plan.platformOffLaunched : copy.plan.platformOff)(copy.setup.accounts[item.platform], formatMoney(item.from_kop), formatMoney(item.min_daily_kop))}</p>
      </div>)}
      {launched ? <>
        <Card className="page-stack"><p>{copy.launchFlow.launchReview}</p><Link className="button button--primary" to="/">{copy.launchFlow.openHome}</Link></Card>
        <PanelCard title={copy.common.budget} titleId="plan-budget"><div className="plan-budget"><BudgetRows plan={plan} /></div></PanelCard>
      </> : <>
        {plan.launch_error && <div className="notice" role="status"><p>{plan.launch_error}</p></div>}
        {plan.blockers.length > 0 && <div className="notice"><p>{copy.launchFlow.blockers}</p><ul>{plan.blockers.map((text) => <li key={text}>{text}</li>)}</ul><Link to="/setup">{copy.common.start}</Link></div>}
        <div className="plan-grid">
          <PanelCard title={copy.plan.groups} titleId="plan-groups" flush className="plan-groups"
            action={<span className="muted">{copy.plan.groupsReady(plan.groups.filter(groupReady).length, plan.groups.length)}</span>}>
            <ul className="plan-groups__list">{plan.groups.map((item) => <li key={item.id}>
              <button type="button" className="plan-group" aria-current={item.id === group?.id || undefined} onClick={() => choose(item.id)}>
                <span className="plan-group__top"><PlatformBadge platform={item.platform} />
                  {groupReady(item) ? <StatusBadge tone="success">{copy.status.ready}</StatusBadge> : <StatusBadge tone="warning">{copy.plan.needImage}</StatusBadge>}</span>
                <span className="plan-group__name">{item.name}</span>
                <span className="plan-group__counts">{copy.plan.groupCounts(item.ads.length, item.assets.filter((asset) => asset.selected).length)}</span>
              </button>
            </li>)}</ul>
            {servicesCut && <p className="plan-groups__note">{copy.plan.directServicesCut(maxDirectGroups)}</p>}
            <div className="plan-budget">
              <p className="plan-label">{copy.plan.budgetTitle(formatMoney(plan.month_budget_kop))}</p>
              {plan.direct_budget_kop > 0 && plan.vk_budget_kop > 0 && <div className="plan-budget__split" aria-hidden="true">
                <i style={{ flexGrow: plan.direct_budget_kop }} /><i style={{ flexGrow: plan.vk_budget_kop }} />
              </div>}
              <BudgetRows plan={plan} />
            </div>
          </PanelCard>
          {forecast && <ForecastPanel forecast={forecast} slow={slow} />}
          {group && <GroupDetail key={group.id} group={group} index={plan.groups.indexOf(group)} total={plan.groups.length} site={business.data?.site_url}
            adIndex={previewAd[group.id] ?? 0} onPreview={(index) => setPreviewAd({ ...previewAd, [group.id]: index })}
            disabled={busy || jobs[0]?.group === group.id} generating={jobState(group.id, 'generate')} uploading={jobState(group.id, 'upload')}
            generationsLeft={generationsLeft}
            onToggle={(id, checked) => {
              const current = planRef.current!;
              update({ ...current, groups: current.groups.map((item) => ({ ...item, assets: item.assets.map((image) => image.id === id ? { ...image, selected: checked } : image) })) });
            }}
            onGenerate={() => enqueue({ group: group.id, kind: 'generate', run: () => regenerateImages(group.id) })}
            onUpload={(file) => enqueue({ group: group.id, kind: 'upload', run: () => uploadPhoto(group.id, file) })}
            onCancel={(kind) => cancel(group.id, kind)} />}
        </div>
        {medical && <div className="plan-medical"><MedicalNotice /><label className="check"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} />{copy.launchFlow.understand}</label></div>}
        {!imagesReady && <p role="status" className="plan-images-missing">{copy.launchFlow.chooseImages}</p>}
        <section className="plan-total" aria-label={copy.plan.total}>
          <div className="plan-total__cell"><p className="plan-total__key">{copy.common.budget}</p>
            <p className="plan-total__value mono">{formatMoney(plan.month_budget_kop)} <small>{copy.plan.perMonth}</small></p></div>
          {(['direct', 'vk'] as const).map((platform) => platformMonth(plan, platform) > 0 && <div key={platform} className="plan-total__cell plan-total__cell--platform">
            <p className="plan-total__key">{copy.launchFlow.checkTitles[platform]}</p><p className="plan-total__value mono">{formatMoney(platformMonth(plan, platform))}</p>
          </div>)}
          {forecast && <div className="plan-total__cell"><p className="plan-total__key">{copy.plan.forecast}{forecast.demo && <span className="plan-demo">{copy.plan.demo}</span>}</p>
            <p className="plan-total__value mono">{forecast.status === 'ready' && forecast.leads_min !== null && forecast.leads_max !== null
              ? <>{copy.plan.forecastLeads(forecast.leads_min, forecast.leads_max)} <small>{copy.plan.forecastShortUnit}</small></>
              : <small>{forecast.status === 'pending' && !slow ? copy.plan.forecastWait : copy.plan.forecastNone}</small>}</p>
          </div>}
          <p className="plan-total__promise"><Icon name="shield" />{copy.plan.promise}</p>
          <div className="plan-total__actions">
            <Link to="/setup/5" className="button button--secondary plan-total__back"><Icon name="back" />{copy.common.back}</Link>
            <Button variant="accent" disabled={busy || jobs.length > 0 || !imagesReady || !plan.ready_to_launch || !plan.marking_confirmed || medical && !acknowledged} onClick={() => { setError(''); setConfirming(true); }}>{busy ? copy.launchFlow.saving : copy.plan.launch}</Button>
          </div>
        </section>
        <ConfirmDialog open={confirming} tone="accent" title={copy.plan.launchTitle} confirm={copy.plan.launchYes} confirming={copy.launchFlow.saving} cancel={copy.plan.launchNo}
          busy={busy} error={confirming && error ? error : undefined} onClose={() => setConfirming(false)}
          onConfirm={() => void act(async () => { await launch(plan); setConfirming(false); })}>
          <ol>{copy.plan.launchNext((['direct', 'vk'] as const).filter((platform) => platformMonth(plan, platform) > 0).map((platform) => copy.setup.accounts[platform]).join(' и '),
            formatMoney(plan.month_budget_kop)).map((text) => <li key={text}>{text}</li>)}</ol>
        </ConfirmDialog>
      </>}
    </>}
  </div>;
}

interface GroupDetailProps {
  group: PlanGroup; index: number; total: number; site?: string; adIndex: number; onPreview: (index: number) => void;
  disabled: boolean; generating?: 'running' | 'queued'; uploading?: 'running' | 'queued'; generationsLeft: number;
  onToggle: (id: number, checked: boolean) => void; onGenerate: () => void; onUpload: (file: File) => void; onCancel: (kind: Job['kind']) => void;
}

function GroupDetail({ group, index, total, site, adIndex, onPreview, disabled, generating, uploading, generationsLeft, onToggle, onGenerate, onUpload, onCancel }: GroupDetailProps) {
  const ad = group.ads[adIndex] ?? group.ads[0];
  const image = group.assets.find((asset) => asset.selected);
  return <PanelCard title={group.name} titleId="plan-group" flush className="plan-detail"
    action={<><PlatformBadge platform={group.platform} /><span className="muted">{copy.plan.groupPosition(index + 1, total)}</span></>}>
    <div className="plan-section"><p className="plan-label">{copy.plan.audience}</p><p>{group.audience}</p></div>
    {ad && <div className="plan-section plan-texts">
      <fieldset className="plan-ads" aria-describedby={group.ads.length > 1 ? `plan-texts-hint-${group.id}` : undefined}>
        <legend className="plan-label">{copy.plan.texts}</legend>
        {group.ads.length > 1 && <p className="muted plan-hint" id={`plan-texts-hint-${group.id}`}>{copy.plan.textsHint}</p>}
        {group.ads.map((item, position) => <label key={position} className="plan-ad">
          <input type="radio" name={`plan-ad-${group.id}`} checked={item === ad} onChange={() => onPreview(position)} />
          <span><strong>{item.title}</strong><span>{item.text}</span></span>
        </label>)}
      </fieldset>
      <div className="plan-preview"><p className="plan-label">{copy.plan.preview}</p>
        <AdPreview platform={group.platform} title={ad.title} text={ad.text} site={site} image={image && { url: image.url, alt: image.alt }} /></div>
    </div>}
    <fieldset disabled={disabled} className="plan-section plan-images"><legend className="plan-label">{copy.plan.imagesSelected(group.assets.filter((asset) => asset.selected).length)}</legend>
      <div className="plan-images__grid">{group.assets.map((asset) => <div key={asset.id} className="plan-image">
        <label className="plan-image__pick">
          <span className="plan-image__pic"><img src={asset.url} alt={asset.alt} />
            <input type="checkbox" checked={asset.selected} onChange={(event) => onToggle(asset.id, event.target.checked)} /></span>
          <span className="plan-image__caption">{asset.kind === 'upload' ? copy.launchFlow.ownPhoto : copy.launchFlow.illustration(asset.label)}</span>
        </label>
        <a className="plan-image__download" href={asset.url} download={`ad-image-${asset.id}.jpg`}>{copy.plan.download}</a>
      </div>)}
        <div className="plan-upload"><input className="visually-hidden" id={`photo-${group.id}`} type="file" accept="image/jpeg,image/png,image/webp" disabled={!!uploading} aria-describedby={`photo-hint-${group.id}`} onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = '';
          if (file) onUpload(file);
        }} /><label htmlFor={`photo-${group.id}`}><Icon name="upload" />{copy.plan.upload}</label></div>
      </div>
      <div className="plan-more">
        <Button variant="secondary" icon="spark" aria-busy={generating === 'running' || undefined} disabled={!!generating || generationsLeft <= 0} onClick={onGenerate}>
          {generating === 'running' ? <span className="loading" role="status">{copy.plan.generating}</span> : generating ? copy.plan.queued : copy.plan.generate}
        </Button>
        {generating === 'queued' && <Button variant="secondary" onClick={() => onCancel('generate')}>{copy.plan.cancelQueued}</Button>}
        <div className="plan-more__text"><p>{copy.launchFlow.remaining(Math.max(0, generationsLeft))}</p><p id={`photo-hint-${group.id}`}>{copy.launchFlow.photoHint}</p></div>
      </div>
      {uploading === 'running' && <p className="loading" role="status">{copy.plan.uploading}</p>}
      {uploading === 'queued' && <div className="actions"><p className="muted" role="status">{copy.plan.uploadQueued}</p><Button variant="secondary" onClick={() => onCancel('upload')}>{copy.plan.cancelQueued}</Button></div>}
    </fieldset>
  </PanelCard>;
}
