import { useCallback, useRef, useState } from 'react';
import { Link } from 'react-router';
import { AdStatusBadge, Button, EmptyState, ErrorBanner, Money, PageHeader, PanelCard, PlatformBadge, StatusBadge } from '../components';
import type { StatusTone } from '../components';
import { copy } from '../copy';
import { getAds, getExperiments, setAdStatus } from '../api/screens';
import { getBusinessToday, lastDays } from '../api/period';
import { displayDate } from '../format';
import { useResource } from '../api/useResource';
import type { Ad, Experiment } from '../api/types';
import { DemoNotice, NullableMoney, adLabels } from './StatsShared';
import './Ads.css';

const armLetters = ['А', 'Б'];
const adsDays = 14;
const experimentTone: Record<Experiment['status'], StatusTone> = { running: 'info', concluded: 'success', cancelled: 'neutral' };
// Настройки рекламы, которые владелец меняет сам: все они в разделе «Настройки», ссылки ведут к нужному блоку.
const changes = [
  { id: 'budget', title: copy.ads.changeBudget, hint: copy.ads.changeBudgetHint },
  { id: 'business', title: copy.ads.changeDescription, hint: copy.ads.changeDescriptionHint },
  { id: 'autopilot', title: copy.ads.changeAutopilot, hint: copy.ads.changeAutopilotHint },
];

function Arms({ experiment }: { experiment: Experiment }) {
  return <ul className="ads-arms">{experiment.arms.map((arm, index) => <li className="ads-arm" key={armLetters[index]}>
    <strong className="ads-arm__letter">{copy.ads.arm(armLetters[index])}</strong>
    {arm.image ? <img src={arm.image} alt={copy.ads.armImage(armLetters[index])} loading="lazy" />
      : experiment.hypothesis === 'image' && <span className="muted">{copy.ads.armNoImage}</span>}
    <span>«{arm.title}». {arm.text}</span>
  </li>)}</ul>;
}

function AdRow({ ad, name, changed }: { ad: Ad; name: string; changed: () => void }) {
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState<string>();
  const [success, setSuccess] = useState(false);
  const canPause = ad.status === 'active';
  const canResume = ad.status === 'paused_auto' || ad.status === 'paused_owner';
  async function toggle() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(undefined); setSuccess(false);
    try { await setAdStatus(ad.id, canPause ? 'pause' : 'resume'); setSuccess(true); changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { lock.current = false; setBusy(false); }
  }
  const stopped = ad.status !== 'active' && ad.status !== 'moderation';
  return <article className={`ads-ad${stopped ? ' ads-ad--stopped' : ''}`}>
    <div className="ads-ad__main">
      {ad.image_url && <img className="ads-ad__image" src={ad.image_url} alt={ad.title} loading="lazy" />}
      <div className="ads-ad__text">
        <h4>{name}</h4>
        <p className="muted">{ad.text}</p>
        <AdStatusBadge status={ad.status} reason={ad.reason} />
      </div>
    </div>
    <dl className="ads-metrics">
      <div><dt>{copy.home.spent}</dt><dd className="mono"><Money kopecks={ad.metrics.cost_kop} /></dd></div>
      <div><dt>{copy.ads.impressions}</dt><dd className="mono">{ad.metrics.impressions.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.ads.clicks}</dt><dd className="mono">{ad.metrics.clicks.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.home.leads}</dt><dd className="mono">{ad.metrics.leads.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.home.leadPrice}</dt><dd className="mono"><NullableMoney value={ad.metrics.cost_per_lead_kop} /></dd></div>
    </dl>
    <div className="ads-ad__action">
      <Button variant="secondary" size="small" icon={canResume ? 'play' : 'pause'} disabled={busy || (!canPause && !canResume)} onClick={() => void toggle()}>
        {busy ? copy.screens.changing : canResume ? copy.common.enable : copy.common.stop}
      </Button>
    </div>
    {error && <div className="ads-ad__note"><ErrorBanner message={error} onRetry={() => void toggle()} /></div>}
    {success && <p className="ads-ad__note ads-ad__done" role="status">{copy.screens.updatedAd}</p>}
  </article>;
}

function ExperimentCard({ experiment }: { experiment: Experiment }) {
  return <article className="ads-experiment">
    <div className="ads-experiment__head"><h3>{experiment.title}</h3>
      <StatusBadge tone={experimentTone[experiment.status]}>{copy.screens.experimentStates[experiment.status]}</StatusBadge></div>
    <p><strong>{copy.ads.comparison}: </strong>{experiment.comparing}</p>
    <Arms experiment={experiment} />
    {experiment.status === 'running' && <p className="muted">{copy.screens.remainingShows(experiment.remaining_impressions)}</p>}
    <p className={`ads-verdict${experiment.conclusion ? ' ads-verdict--done' : ''}`}><strong>{copy.ads.conclusion}: </strong>
      {experiment.conclusion ?? (experiment.status === 'running' ? copy.common.insufficient : copy.common.unknown)}</p>
  </article>;
}

export function AdsPage() {
  const ads = useResource(useCallback(async (signal: AbortSignal) => {
    // Демо-сервер не сказал свою дату — период выберет сервер объявлений: те же 14 дней по дате бизнеса.
    const today = await getBusinessToday(signal).catch(() => null);
    const period = today ? lastDays(adsDays, today) : null;
    return { ...await getAds(signal, period ?? undefined), period };
  }, []));
  const experiments = useResource(getExperiments);
  const items = ads.data?.items ?? [];
  const platforms = [...new Set(items.map((item) => item.platform))];
  const names = adLabels(items);
  const period = ads.data?.period;
  const dates = period ? copy.screens.dates(displayDate(period.from), displayDate(period.to)) : copy.screens.recentDays(adsDays);
  // Причина остановки — о цифрах на момент решения, а в строках цифры за последние дни: подписываем период.
  const stoppedByUs = items.some((ad) => ad.status === 'paused_auto' && ad.reason);
  return <div className="page-stack">
    <PageHeader title={copy.ads.title} description={copy.screens.adIntro} />
    <DemoNotice />
    {ads.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {ads.error && <ErrorBanner message={ads.error} onRetry={ads.reload} />}
    {ads.data && items.length === 0 && <div className="panel"><EmptyState title={copy.screens.noAds} description={copy.screens.noAdsHint}
      action={<Link className="button button--primary" to="/setup">{copy.common.start}</Link>} /></div>}
    {items.length > 0 && <div className="ads-legend">
      <p>{(stoppedByUs ? copy.screens.adPeriodStopped : copy.screens.adPeriod)(dates)}</p>
      <p className="muted">{copy.ads.toggleHint}</p>
    </div>}
    {items.length > 0 && <section className="ads-changes" aria-labelledby="ads-changes-title">
      <h2 id="ads-changes-title" className="ads-kicker">{copy.ads.changeTitle}</h2>
      <ul>{changes.map((item) => <li key={item.id}><Link to={`/settings#settings-${item.id}-title`}>{item.title} <span aria-hidden="true">→</span></Link>
        <span className="muted">{item.hint}</span></li>)}</ul>
    </section>}
    {platforms.map((platform) => {
      const platformAds = items.filter((ad) => ad.platform === platform);
      const campaigns = [...new Set(platformAds.map((ad) => ad.campaign_id))];
      return <PanelCard key={platform} title={<PlatformBadge platform={platform} />} titleId={`ads-${platform}`} flush
        action={<span className="muted">{copy.ads.count(platformAds.length)}</span>}>
        {campaigns.map((campaignId) => {
          const campaignAds = platformAds.filter((ad) => ad.campaign_id === campaignId);
          const groups = [...new Set(campaignAds.map((ad) => ad.group_id))];
          return <section className="ads-campaign" key={campaignId} aria-labelledby={`campaign-${campaignId}`}>
            <h3 id={`campaign-${campaignId}`}><span className="ads-kicker">{copy.ads.campaign}</span>{campaignAds[0].campaign_name}</h3>
            {groups.map((groupId) => {
              const groupAds = campaignAds.filter((ad) => ad.group_id === groupId);
              return <div className="ads-group" key={groupId}>
                <p className="ads-group__name"><span className="ads-kicker">{copy.ads.group}</span>{groupAds[0].group_name}</p>
                {groupAds.map((ad) => <AdRow key={ad.id} ad={ad} name={names.get(ad.id) ?? ad.title} changed={ads.reload} />)}
              </div>;
            })}
          </section>;
        })}
      </PanelCard>;
    })}
    <PanelCard title={copy.ads.experiments} titleId="ads-experiments">
      <div className="page-stack">
        <p className="muted">{copy.screens.experimentExplanation}</p>
        {experiments.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
        {experiments.error && <ErrorBanner message={experiments.error} onRetry={experiments.reload} />}
        {experiments.data && (experiments.data.items.length
          ? experiments.data.items.map((experiment) => <ExperimentCard key={experiment.id} experiment={experiment} />)
          : <EmptyState title={copy.screens.noExperiments} description={copy.screens.experimentHint} />)}
      </div>
    </PanelCard>
  </div>;
}
