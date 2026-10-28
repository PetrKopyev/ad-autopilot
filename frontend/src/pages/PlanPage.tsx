import { useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { formatMoney } from '../components/Money';
import { getPlan, launchPlan, regenerateImages, saveAssets, uploadPhoto } from '../api/plan';
import type { Plan } from '../api/plan';
import { useResource } from '../api/useResource';
import { copy } from '../copy';
import { MedicalNotice } from './SetupShared';
import './Setup.css';

interface Job { group: number; kind: 'generate' | 'upload'; run: () => Promise<Plan> }

function selectedIds(plan: Plan) {
  return plan.groups.flatMap((group) => group.assets.filter((asset) => asset.selected).map((asset) => asset.id));
}

// Сервер отвечает всем планом; отметки, которые поменяли, пока шёл запрос, важнее отметок из ответа.
function merge(local: Plan, fresh: Plan): Plan {
  const selected = new Map(local.groups.flatMap((group) => group.assets.map((asset) => [asset.id, asset.selected] as const)));
  return { ...fresh, groups: fresh.groups.map((group) => ({ ...group, assets: group.assets.map((asset) => ({ ...asset, selected: selected.get(asset.id) ?? asset.selected })) })) };
}

export function PlanPage() {
  const resource = useResource(getPlan);
  const [edited, setEdited] = useState<Plan>();
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [jobs, setJobs] = useState<Job[]>([]);
  // Сервер держит блокировку бизнеса всю генерацию: запросы картинок идут по одному, остальные ждут в очереди.
  const queue = useRef<Job[]>([]);
  const plan = edited ?? resource.data;
  const planRef = useRef(plan);
  planRef.current = plan;
  const medical = !!plan && plan.medical_level !== 'none';
  const imagesReady = !!plan?.groups.length && plan.groups.every((group) => group.assets.some((asset) => asset.selected));
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
  return <div className="page-stack launch-flow">
    <h1>{plan?.status === 'launched' ? copy.launchFlow.launched : copy.plan.title}</h1>
    {resource.loading && <p className="loading" role="status">{copy.launchFlow.loading}</p>}
    {resource.error && <ErrorBanner message={resource.error} onRetry={resource.reload} />}
    {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    {plan && <>
      <p>{copy.plan.summary(formatMoney(plan.month_budget_kop))}</p>
      {(['direct', 'vk'] as const).map((platform) => { const daily = plan.daily_kop[platform]; return daily !== undefined && <p key={platform}>
        {copy.plan.platformBudget(copy.setup.accounts[platform], formatMoney(platform === 'direct' ? plan.direct_budget_kop : plan.vk_budget_kop), formatMoney(daily))}
      </p>; })}
      {plan.off.map((item) => <div key={item.platform} className="notice" role="status">
        <p>{copy.plan.platformOff(copy.setup.accounts[item.platform], formatMoney(item.from_kop), formatMoney(item.min_daily_kop))}</p>
      </div>)}
      {plan.status === 'launched' ? <Card className="page-stack"><p>{copy.launchFlow.launchReview}</p><Link className="button button--primary" to="/">{copy.launchFlow.openHome}</Link></Card> : <>
        {plan.launch_error && <div className="notice" role="status"><p>{plan.launch_error}</p></div>}
        {(!plan.marking_confirmed || !plan.ready_to_launch) && <div className="notice"><p>{copy.plan.marking}</p><p>{copy.launchFlow.notReady}</p><Link to="/setup">{copy.common.start}</Link></div>}
        {medical && <><MedicalNotice /><label className="check"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} />{copy.launchFlow.understand}</label></>}
        {plan.groups.map((group) => { const generating = jobState(group.id, 'generate'); const uploading = jobState(group.id, 'upload'); return <Card key={group.id} className="page-stack">
          <h2>{group.name}</h2><p className="muted">{copy.setup.accounts[group.platform]}</p>
          <div><h2>{copy.plan.audience}</h2><p>{group.audience}</p></div>
          <h2>{copy.plan.texts}</h2>{group.ads.map((ad, index) => <div key={index}><strong>{ad.title}</strong><p>{ad.text}</p></div>)}
          <fieldset disabled={busy || jobs[0]?.group === group.id} className="page-stack"><legend>{copy.plan.images}</legend>
            <div className="image-grid">{group.assets.map((asset) => <div key={asset.id} className="image-choice">
              <label className="image-pick">
                <img src={asset.url} alt={asset.alt} />
                <span className="check"><input type="checkbox" checked={asset.selected} onChange={(event) => {
                  const current = planRef.current!; const checked = event.target.checked;
                  update({ ...current, groups: current.groups.map((item) => ({ ...item, assets: item.assets.map((image) => image.id === asset.id ? { ...image, selected: checked } : image) })) });
                }} /><span>{asset.kind === 'upload' ? copy.launchFlow.ownPhoto : copy.launchFlow.illustration(asset.label)}</span></span>
              </label>
              <a className="image-download" href={asset.url} download={`ad-image-${asset.id}.jpg`}>{copy.plan.download}</a>
            </div>)}</div>
            <p className="muted">{copy.launchFlow.remaining(Math.max(0, generationsLeft))}</p>
            <div className="actions"><Button variant="secondary" aria-busy={generating === 'running' || undefined} disabled={!!generating || generationsLeft <= 0} onClick={() => enqueue({ group: group.id, kind: 'generate', run: () => regenerateImages(group.id) })}>
              {generating === 'running' ? <span className="loading" role="status">{copy.plan.generating}</span> : generating ? copy.plan.queued : copy.plan.generate}
            </Button>{generating === 'queued' && <Button variant="secondary" onClick={() => cancel(group.id, 'generate')}>{copy.plan.cancelQueued}</Button>}</div>
            <div className="photo-upload"><input className="file-input sr-only" id={`photo-${group.id}`} type="file" accept="image/jpeg,image/png,image/webp" disabled={!!uploading} aria-describedby={`photo-hint-${group.id}`} onChange={(event) => {
              const file = event.target.files?.[0]; event.target.value = '';
              if (file) enqueue({ group: group.id, kind: 'upload', run: () => uploadPhoto(group.id, file) });
            }} /><label className="button button--secondary" htmlFor={`photo-${group.id}`}>{copy.plan.upload}</label></div>
            {uploading === 'running' && <p className="loading" role="status">{copy.plan.uploading}</p>}
            {uploading === 'queued' && <div className="actions"><p className="muted" role="status">{copy.plan.uploadQueued}</p><Button variant="secondary" onClick={() => cancel(group.id, 'upload')}>{copy.plan.cancelQueued}</Button></div>}
            <p className="muted" id={`photo-hint-${group.id}`}>{copy.launchFlow.photoHint}</p>
          </fieldset>
        </Card>; })}
        {!imagesReady && <p role="status">{copy.launchFlow.chooseImages}</p>}
        <Card className="page-stack"><div className="actions">
          <Link to="/setup/5" className="button button--secondary">{copy.common.back}</Link>
          <Button disabled={busy || jobs.length > 0 || !imagesReady || !plan.ready_to_launch || !plan.marking_confirmed || medical && !acknowledged} onClick={() => void act(() => launch(plan))}>{busy ? copy.launchFlow.saving : copy.plan.launch}</Button>
        </div><p>{copy.plan.promise}</p></Card>
      </>}
    </>}
  </div>;
}
