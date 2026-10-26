import { useState } from 'react';
import { Link } from 'react-router';
import { Button, Card, ErrorBanner } from '../components';
import { formatMoney } from '../components/Money';
import { getPlan, launchPlan, regenerateImages, saveAssets, uploadPhoto } from '../api/plan';
import type { Plan } from '../api/plan';
import { useResource } from '../api/useResource';
import { copy } from '../copy';
import { MedicalNotice } from './SetupShared';
import './Setup.css';

export function PlanPage() {
  const resource = useResource(getPlan);
  const [edited, setEdited] = useState<Plan>();
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const plan = edited ?? resource.data;
  const medical = !!plan && plan.medical_level !== 'none';
  const selected = plan?.groups.flatMap((group) => group.assets.filter((asset) => asset.selected).map((asset) => asset.id)) ?? [];
  const imagesReady = !!plan?.groups.length && plan.groups.every((group) => group.assets.some((asset) => asset.selected));
  async function act(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError('');
    try { await action(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { setBusy(false); }
  }
  async function updateImages(action: () => Promise<Plan>) {
    await saveAssets(selected); setEdited(await action());
  }
  return <div className="page-stack launch-flow">
    <h1>{copy.plan.title}</h1>
    {resource.loading && <p role="status">{copy.launchFlow.loading}</p>}
    {resource.error && <ErrorBanner message={resource.error} onRetry={resource.reload} />}
    {error && <ErrorBanner message={error} onDismiss={() => setError('')} />}
    {plan && <>
      <p>{copy.plan.summary(formatMoney(plan.month_budget_kop), formatMoney(plan.direct_budget_kop), formatMoney(plan.vk_budget_kop))}</p>
      {plan.status === 'launched' ? <Card className="page-stack"><h2>{copy.launchFlow.launched}</h2><Link to="/">{copy.launchFlow.openHome}</Link></Card> : <>
        {(!plan.marking_confirmed || !plan.ready_to_launch) && <div className="notice"><p>{copy.plan.marking}</p><p>{copy.launchFlow.notReady}</p><Link to="/setup">{copy.common.start}</Link></div>}
        {medical && <><MedicalNotice /><label className="check"><input type="checkbox" checked={acknowledged} disabled={busy} onChange={(event) => setAcknowledged(event.target.checked)} />{copy.launchFlow.understand}</label></>}
        {plan.groups.map((group) => <Card key={group.id} className="page-stack">
          <h2>{group.name}</h2><p className="muted">{copy.setup.accounts[group.platform]}</p>
          <div><h2>{copy.plan.audience}</h2><p>{group.audience}</p></div>
          <p>{copy.plan.dailySpend}: {formatMoney(group.daily_limit_kop)}</p>
          <h2>{copy.plan.texts}</h2>{group.ads.map((ad, index) => <div key={index}><strong>{ad.title}</strong><p>{ad.text}</p></div>)}
          <fieldset disabled={busy} className="page-stack"><legend>{copy.plan.images}</legend>
            <div className="image-grid">{group.assets.map((asset) => <label key={asset.id} className="image-choice">
              <img src={asset.url} alt={asset.alt} />
              <span className="check"><input type="checkbox" checked={asset.selected} onChange={(event) => {
                setEdited({ ...plan, groups: plan.groups.map((item) => ({ ...item, assets: item.assets.map((image) => image.id === asset.id ? { ...image, selected: event.target.checked } : image) })) });
              }} /><span>{asset.kind === 'upload' ? copy.launchFlow.ownPhoto : copy.launchFlow.illustration}</span></span>
            </label>)}</div>
            <p className="muted">{copy.plan.generationLimit}. {copy.launchFlow.remaining(plan.generations_remaining)}</p>
            <div className="actions"><Button variant="secondary" disabled={busy || plan.generations_remaining <= 0} onClick={() => void act(() => updateImages(() => regenerateImages(group.id)))}>{copy.plan.generate}</Button></div>
            <label className="field">{copy.plan.upload}<input type="file" accept="image/jpeg,image/png,image/webp" aria-describedby={`photo-hint-${group.id}`} onChange={(event) => {
              const file = event.target.files?.[0]; event.target.value = '';
              if (file) void act(() => updateImages(() => uploadPhoto(group.id, file)));
            }} /></label><p className="muted" id={`photo-hint-${group.id}`}>{copy.launchFlow.photoHint}</p>
          </fieldset>
        </Card>)}
        {!imagesReady && <p role="status">{copy.launchFlow.chooseImages}</p>}
        <Card className="page-stack"><div className="actions">
          <Link to="/setup/5" className="button button--secondary">{copy.common.back}</Link>
          <Button disabled={busy || !imagesReady || !plan.ready_to_launch || !plan.marking_confirmed || medical && !acknowledged} onClick={() => void act(async () => {
            await saveAssets(selected); await launchPlan(plan.id, acknowledged); setEdited({ ...plan, status: 'launched' });
          })}>{busy ? copy.launchFlow.saving : copy.plan.launch}</Button>
        </div><p>{copy.plan.promise}</p></Card>
      </>}
    </>}
  </div>;
}
