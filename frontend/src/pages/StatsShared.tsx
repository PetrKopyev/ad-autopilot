import { Icon, MetricTile, Money, PanelCard, SegmentedControl, formatMoney } from '../components';
import { copy } from '../copy';
import type { PeriodChoice } from '../api/period';
import { displayDate, displayDateTime } from '../format';
import { mockEnabled } from '../api/screens';
import { useDemo } from '../api/useDemo';
import type { Ad, AdStatus, Campaign, Experiment, Source, Summary } from '../api/types';
import './Stats.css';

export function DemoNotice() { return mockEnabled ? <p className="stats-demo">{copy.screens.demo}</p> : null; }
export function PeriodPicker({ value, onChange }: { value: PeriodChoice; onChange: (value: PeriodChoice) => void }) {
  return <SegmentedControl label={copy.statistics.period} value={value} onChange={onChange}
    options={(Object.keys(copy.screens.periods) as PeriodChoice[]).map((period) => ({ value: period, label: copy.screens.periods[period], shortLabel: copy.screens.shortPeriods[period] }))} />;
}
export function NullableMoney({ value }: { value: number | null }) {
  return value === null ? <span className="muted">{copy.screens.noPrice}</span> : <Money kopecks={value} />;
}
const tileMoney = (value: number | null) => value === null ? '—' : formatMoney(value);
export function SummaryFigures({ summary }: { summary: Summary }) {
  return <div className="metric-tiles stats-figures">
    <MetricTile label={copy.home.spent} value={formatMoney(summary.cost_kop)} />
    <MetricTile label={copy.home.leads} value={summary.leads.toLocaleString('ru-RU')} />
    <MetricTile label={copy.home.leadPrice} value={tileMoney(summary.cost_per_lead_kop)} hint={summary.cost_per_lead_kop === null ? copy.screens.noPrice : undefined} />
    <MetricTile label={copy.home.clients} value={summary.clients?.toLocaleString('ru-RU') ?? '—'} hint={summary.clients === null ? copy.statistics.clientsHint : undefined} />
    <MetricTile label={copy.home.clientPrice} value={tileMoney(summary.cost_per_client_kop)} hint={summary.cost_per_client_kop === null ? copy.screens.noPrice : undefined} />
  </div>;
}
export function ClientsSummary({ summary }: { summary: Summary }) {
  const window = summary.clients_window;
  return <PanelCard title={copy.statistics.clients} titleId="stats-clients" action={<span className="muted mono">{copy.screens.dates(displayDate(window.from), displayDate(window.to))}</span>}>
    <div className="stats-clients">
      <p>{copy.screens.clientsWindow}</p>
      {window.clients === null ? <p>{copy.screens.noClients}. {copy.statistics.clientsHint}</p> : <dl className="stats-clients__figures">
        <div><dt>{copy.home.clients}</dt><dd className="mono">{window.clients.toLocaleString('ru-RU')}</dd></div>
        <div><dt>{copy.home.clientPrice}</dt><dd className="mono"><NullableMoney value={window.cost_per_client_kop} /></dd></div>
      </dl>}
      <p className="muted">{copy.screens.answeredWeeks(window.answered_weeks, window.unknown_weeks)}</p>
    </div>
  </PanelCard>;
}
export function platformName(platform: string): string {
  return copy.screens.platforms[platform as keyof typeof copy.screens.platforms] ?? platform;
}
export function Sources({ sources }: { sources: Source[] }) {
  const demo = useDemo();
  return <div className="stats-sources">
    <div className="stats-sources__text">
      <p className="stats-sources__pending"><Icon name="history" />{copy.statistics.provisional}</p>
      {sources.length === 0 && <p>{copy.screens.noSources}</p>}
      {sources.map((source) => <p key={source.platform}>{copy.statistics.source(platformName(source.platform), displayDateTime(source.fetched_at))}</p>)}
    </div>
    <div className="stats-sources__links">
      {demo && sources.length > 0 && <p className="muted">{copy.screens.compareDemo}</p>}
      {!demo && sources.some((source) => source.platform === 'direct') && <a href="https://direct.yandex.ru/" target="_blank" rel="noreferrer">{copy.screens.compareDirect}<Icon name="arrow" /></a>}
      {!demo && sources.some((source) => source.platform === 'vk') && <a href="https://ads.vk.com/" target="_blank" rel="noreferrer">{copy.screens.compareVk}<Icon name="arrow" /></a>}
    </div>
  </div>;
}

const stoppedCampaign = new Set(['paused_owner', 'paused_auto', 'archived']);
/** Остановленная кампания не показывает ни одного своего объявления: включённые в ней объявления — со статусом кампании. */
export function shownStatus(ad: Ad, campaigns: Campaign[]): AdStatus {
  const campaign = campaigns.find((item) => item.id === ad.campaign_id);
  return campaign && stoppedCampaign.has(campaign.status) && (ad.status === 'active' || ad.status === 'moderation') ? campaign.status as AdStatus : ad.status;
}
export const campaignStopped = (id: number, campaigns: Campaign[]) => stoppedCampaign.has(campaigns.find((item) => item.id === id)?.status ?? '');

/** Подпись к выводу сравнения: за какие дни его цифры. */
export function experimentPeriod({ period, status }: Experiment): string | null {
  return period && copy.ads.period(displayDate(period.from), displayDate(period.to), status === 'running');
}

/** Меньше трёх работающих объявлений: опорная цена — средняя по всей рекламе, решения ждут обращений (как ref_cpl на сервере). */
export function fewAdsReason(ads: Ad[], campaigns: Campaign[], avgCheck: number | null): string | null {
  const live = ads.filter((ad) => shownStatus(ad, campaigns) === 'active').length;
  return live > 0 && live < 3 ? copy.screens.fewAds(live, avgCheck !== null) : null;
}

function excerpt(text: string): string {
  const clean = text.trim().replace(/\s+/g, ' ');
  if (clean.length <= 40) return clean;
  return `${clean.slice(0, 40).replace(/\s+\S*$/, '')}…`;
}
/** Названия объявлений для владельца: одинаковые заголовки различаем по тексту, а одинаковые и по тексту — по номеру варианта. */
export function adLabels(ads: Ad[]): Map<number, string> {
  const count = (key: (ad: Ad) => string) => ads.reduce((map, ad) => map.set(key(ad), (map.get(key(ad)) ?? 0) + 1), new Map<string, number>());
  const titles = count((ad) => ad.title);
  const texts = count((ad) => `${ad.title}\n${ad.text}`);
  const seen = new Map<string, number>();
  return new Map([...ads].sort((a, b) => a.id - b.id).map((ad): [number, string] => {
    if (titles.get(ad.title) === 1) return [ad.id, ad.title];
    const key = `${ad.title}\n${ad.text}`;
    const label = `${ad.title} — «${excerpt(ad.text)}»`;
    if (texts.get(key) === 1) return [ad.id, label];
    const index = (seen.get(key) ?? 0) + 1;
    seen.set(key, index);
    // Текст тот же, а картинки разные — так их и различаем: это варианты сравнения картинок.
    return [ad.id, (ad.image_url ? copy.screens.adImageVariant : copy.screens.adVariant)(label, index)];
  }));
}
