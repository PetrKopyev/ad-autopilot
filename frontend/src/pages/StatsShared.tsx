import { Button, Card, Money } from '../components';
import { copy } from '../copy';
import type { PeriodChoice } from '../api/period';
import { displayDate, displayDateTime } from '../format';
import { mockEnabled } from '../api/screens';
import type { Ad, Source, Summary } from '../api/types';
import './Stats.css';

export function DemoNotice() { return mockEnabled ? <p className="stats-demo">{copy.screens.demo}</p> : null; }
export function PeriodPicker({ value, onChange }: { value: PeriodChoice; onChange: (value: PeriodChoice) => void }) {
  return <div className="period-picker" role="group" aria-label={copy.statistics.period}>
    {(Object.keys(copy.screens.periods) as PeriodChoice[]).map((period) => <Button key={period}
      variant={value === period ? 'primary' : 'secondary'} aria-pressed={value === period}
      onClick={() => onChange(period)}><span className="period-label-full">{copy.screens.periods[period]}</span><span className="period-label-short">{copy.screens.shortPeriods[period]}</span></Button>)}
  </div>;
}
export function NullableMoney({ value }: { value: number | null }) {
  return value === null ? <span className="muted">{copy.screens.noPrice}</span> : <Money kopecks={value} />;
}
export function SummaryFigures({ summary }: { summary: Summary }) {
  return <div className="stats-figures">
    <Card><p className="muted">{copy.home.spent}</p><p className="stats-value"><Money kopecks={summary.cost_kop} /></p></Card>
    <Card><p className="muted">{copy.home.leads}</p><p className="stats-value">{summary.leads.toLocaleString('ru-RU')}</p></Card>
    <Card><p className="muted">{copy.home.clients}</p><p className="stats-value">{summary.clients ?? '—'}</p></Card>
    <Card><p className="muted">{copy.home.leadPrice}</p><p className="stats-value"><NullableMoney value={summary.cost_per_lead_kop} /></p></Card>
  </div>;
}
export function ClientsSummary({ summary }: { summary: Summary }) {
  const window = summary.clients_window;
  return <Card className="page-stack"><h2>{copy.statistics.clients}</h2>
    <p>{copy.screens.clientsWindow}</p><p className="muted">{copy.screens.dates(displayDate(window.from), displayDate(window.to))}</p>
    {window.clients === null ? <p>{copy.screens.noClients}. {copy.statistics.clientsHint}</p> : <div className="actions">
      <p>{copy.home.clients}: <strong>{window.clients}</strong></p>
      <p>{copy.home.clientPrice}: <NullableMoney value={window.cost_per_client_kop} /></p>
    </div>}
    <p className="muted">{copy.screens.answeredWeeks(window.answered_weeks, window.unknown_weeks)}</p>
  </Card>;
}
export function platformName(platform: string): string {
  return copy.screens.platforms[platform as keyof typeof copy.screens.platforms] ?? platform;
}
export function Sources({ sources }: { sources: Source[] }) {
  return <div className="stats-sources">
    <p>{copy.statistics.provisional}</p>
    {sources.length === 0 && <p>{copy.screens.noSources}</p>}
    {sources.map((source) => <p key={source.platform}>{copy.statistics.source(platformName(source.platform), displayDateTime(source.fetched_at))}</p>)}
    <div className="actions">
      {sources.some((source) => source.platform === 'direct') && <a href="https://direct.yandex.ru/" target="_blank" rel="noreferrer">{copy.screens.compareDirect}</a>}
      {sources.some((source) => source.platform === 'vk') && <a href="https://ads.vk.com/" target="_blank" rel="noreferrer">{copy.screens.compareVk}</a>}
    </div>
  </div>;
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
    return [ad.id, copy.screens.adVariant(label, index)];
  }));
}
