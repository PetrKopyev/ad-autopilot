import { useCallback, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, Card, EmptyState, ErrorBanner, Money, StatusBadge } from '../components';
import { copy } from '../copy';
import { getAds, getExperiments, setAdStatus } from '../api/screens';
import { getBusinessToday, lastDays } from '../api/period';
import { displayDate } from '../format';
import { useResource } from '../api/useResource';
import type { Ad, Experiment } from '../api/types';
import { DemoNotice, NullableMoney, adLabels, platformName } from './StatsShared';
import './Ads.css';

const armLetters = ['А', 'Б'];
const adsDays = 14;

function Arms({ experiment }: { experiment: Experiment }) {
  return <ul className="ads-arms">{experiment.arms.map((arm, index) => <li className="ads-arm" key={armLetters[index]}>
    <strong>{copy.ads.arm(armLetters[index])}</strong>
    {arm.image ? <img src={arm.image} alt={copy.ads.armImage(armLetters[index])} loading="lazy" />
      : experiment.hypothesis === 'image' && <span className="muted">{copy.ads.armNoImage}</span>}
    <span>«{arm.title}». {arm.text}</span>
  </li>)}</ul>;
}

function AdCard({ ad, name, dates, changed }: { ad: Ad; name: string; dates: string; changed: () => void }) {
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState<string>();
  const [success, setSuccess] = useState(false);
  const canPause = ad.status === 'active';
  const canResume = ad.status === 'paused_auto' || ad.status === 'paused_owner';
  const label = copy.screens.adStates[ad.status] ?? copy.screens.draft;
  async function toggle() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(undefined); setSuccess(false);
    try { await setAdStatus(ad.id, canPause ? 'pause' : 'resume'); setSuccess(true); changed(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : copy.errors.generic); }
    finally { lock.current = false; setBusy(false); }
  }
  return <article className="ads-ad page-stack">
    <div className="ads-ad-heading"><h3>{name}</h3><StatusBadge tone={ad.status === 'active' ? 'success' : ad.status === 'rejected' ? 'danger' : 'neutral'}>
      {ad.reason ? copy.screens.because(label, ad.reason) : label}</StatusBadge></div>
    {ad.image_url && <img className="ads-image" src={ad.image_url} alt={ad.title} loading="lazy" />}
    <p>{ad.text}</p>
    {/* Причина остановки — о цифрах на момент решения, а ниже цифры за последние дни: подписываем период. */}
    <p className="muted">{(ad.status === 'paused_auto' && ad.reason ? copy.screens.adPeriodStopped : copy.screens.adPeriod)(dates)}</p>
    <dl className="ads-metrics">
      <div><dt>{copy.home.spent}</dt><dd><Money kopecks={ad.metrics.cost_kop} /></dd></div>
      <div><dt>{copy.ads.impressions}</dt><dd>{ad.metrics.impressions.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.ads.clicks}</dt><dd>{ad.metrics.clicks.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.home.leads}</dt><dd>{ad.metrics.leads.toLocaleString('ru-RU')}</dd></div>
      <div><dt>{copy.home.leadPrice}</dt><dd><NullableMoney value={ad.metrics.cost_per_lead_kop} /></dd></div>
    </dl>
    {error && <ErrorBanner message={error} onRetry={() => void toggle()} />}
    {success && <p role="status">{copy.screens.updatedAd}</p>}
    <div className="actions ads-actions">
      <Button variant="secondary" disabled={busy || (!canPause && !canResume)} onClick={() => void toggle()}>
        {busy ? copy.screens.changing : canResume ? copy.common.enable : copy.common.stop}
      </Button>
    </div>
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
  return <div className="page-stack">
    <div className="page-heading"><h1>{copy.ads.title}</h1><p className="muted">{copy.screens.adIntro}</p></div>
    <DemoNotice />
    {ads.loading && <p role="status">{copy.screens.loading}</p>}
    {ads.error && <ErrorBanner message={ads.error} onRetry={ads.reload} />}
    {ads.data && items.length === 0 && <Card><EmptyState title={copy.screens.noAds} description={copy.screens.noAdsHint}
      action={<Link className="button button--primary" to="/setup">{copy.common.start}</Link>} /></Card>}
    {platforms.map((platform) => {
      const platformAds = items.filter((ad) => ad.platform === platform);
      const campaigns = [...new Set(platformAds.map((ad) => ad.campaign_id))];
      return <Card className="page-stack" key={platform}><h2>{platformName(platform)}</h2>
        {campaigns.map((campaignId) => {
          const campaignAds = platformAds.filter((ad) => ad.campaign_id === campaignId);
          const groups = [...new Set(campaignAds.map((ad) => ad.group_id))];
          return <details className="ads-branch" key={campaignId} open><summary>{campaignAds[0].campaign_name}</summary>
            {groups.map((groupId) => {
              const groupAds = campaignAds.filter((ad) => ad.group_id === groupId);
              return <details className="ads-branch ads-group" key={groupId} open><summary>{groupAds[0].group_name}</summary>
                <div className="page-stack">{groupAds.map((ad) => <AdCard key={ad.id} ad={ad} name={names.get(ad.id) ?? ad.title} dates={dates} changed={ads.reload} />)}</div>
              </details>;
            })}
          </details>;
        })}
      </Card>;
    })}
    <Card className="page-stack"><h2>{copy.ads.experiments}</h2><p className="muted">{copy.screens.experimentExplanation}</p>
      {experiments.loading && <p role="status">{copy.screens.loading}</p>}
      {experiments.error && <ErrorBanner message={experiments.error} onRetry={experiments.reload} />}
      {experiments.data && (experiments.data.items.length ? experiments.data.items.map((experiment) => <article className="ads-ad page-stack" key={experiment.id}>
        <h3>{experiment.title}</h3><StatusBadge>{copy.screens.experimentStates[experiment.status]}</StatusBadge>
        <p><strong>{copy.ads.comparison}: </strong>{experiment.comparing}</p>
        <Arms experiment={experiment} />
        {experiment.status === 'running' && <p>{copy.screens.remainingShows(experiment.remaining_impressions)}</p>}
        <p><strong>{copy.ads.conclusion}: </strong>{experiment.conclusion ?? (experiment.status === 'running' ? copy.common.insufficient : copy.common.unknown)}</p>
      </article>) : <EmptyState title={copy.screens.noExperiments} description={copy.screens.experimentHint} />)}
    </Card>
  </div>;
}
