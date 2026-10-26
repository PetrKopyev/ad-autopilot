import { useCallback, useState } from 'react';
import { Card, EmptyState, ErrorBanner, Money } from '../components';
import { copy } from '../copy';
import { getBreakdown, getDaily, getSummary } from '../api/screens';
import { displayDate, getPeriod, type PeriodChoice } from '../api/period';
import { useResource } from '../api/useResource';
import type { Daily, StatsLevel } from '../api/types';
import { ClientsSummary, DemoNotice, NullableMoney, PeriodPicker, Sources, SummaryFigures, platformName } from './StatsShared';

function DailyChart({ daily }: { daily: Daily }) {
  const points = daily.items;
  const maxCost = Math.max(100, ...points.map((point) => point.cost_kop));
  const maxLeads = Math.max(1, ...points.map((point) => point.leads));
  const x = (index: number) => points.length === 1 ? 350 : 60 + index * 580 / (points.length - 1);
  const y = (value: number, max: number) => 210 - value / max * 160;
  const line = (metric: 'cost_kop' | 'leads', max: number) => points.map((point, index) => `${x(index)},${y(point[metric], max)}`).join(' ');
  const hasData = points.some((point) => point.sources.length > 0);
  if (!hasData) return <EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} />;
  return <>
    <div className="stats-chart-key"><span>{copy.screens.chartSpend}</span><span>{copy.screens.chartLeads}</span></div>
    <p className="muted">{copy.screens.chartExplanation}</p>
    <svg className="stats-chart" viewBox="0 0 700 260" role="img" aria-label={copy.statistics.chart}>
      <title>{copy.statistics.chart}</title>
      {[0, 0.5, 1].map((fraction) => <g key={fraction}>
        <line x1="60" x2="640" y1={y(fraction, 1)} y2={y(fraction, 1)} stroke="#dce2e9" />
        <text x="52" y={y(fraction, 1) + 4} textAnchor="end">{(maxCost * fraction / 100).toLocaleString('ru-RU', { maximumFractionDigits: 2 })}</text>
        <text x="648" y={y(fraction, 1) + 4}>{(maxLeads * fraction).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}</text>
      </g>)}
      <text x="60" y="24">{copy.screens.chartSpend}</text><text x="640" y="24" textAnchor="end">{copy.screens.chartLeads}</text>
      <polyline points={line('cost_kop', maxCost)} fill="none" stroke="#245e4c" strokeWidth="3" strokeLinejoin="round" />
      <polyline points={line('leads', maxLeads)} fill="none" stroke="#326bc0" strokeWidth="3" strokeLinejoin="round" strokeDasharray="7 4" />
      {points.map((point, index) => <g key={point.date}>
        <circle cx={x(index)} cy={y(point.cost_kop, maxCost)} r="3" fill="#245e4c" />
        <circle cx={x(index)} cy={y(point.leads, maxLeads)} r="3" fill="#326bc0" />
      </g>)}
      <text x="60" y="242">{displayDate(points[0].date)}</text><text x="640" y="242" textAnchor="end">{displayDate(points[points.length - 1].date)}</text>
    </svg>
    <details className="stats-chart-details"><summary>{copy.screens.chartDetails}</summary>
      <div className="stats-table-scroll" tabIndex={0} role="region" aria-label={copy.screens.chartDetails}><table className="stats-table">
        <thead><tr><th scope="col">{copy.screens.day}</th><th scope="col">{copy.home.spent}</th><th scope="col">{copy.home.leads}</th></tr></thead>
        <tbody>{points.map((point) => <tr key={point.date}><th scope="row">{displayDate(point.date)}{point.is_incomplete ? ' *' : ''}</th>
          <td><Money kopecks={point.cost_kop} /></td><td>{point.leads}</td></tr>)}</tbody>
      </table></div>
    </details>
  </>;
}

export function StatsPage() {
  const [choice, setChoice] = useState<PeriodChoice>('month');
  const [level, setLevel] = useState<StatsLevel>('platform');
  const summary = useResource(useCallback((signal: AbortSignal) => getSummary(getPeriod(choice), signal), [choice]));
  const daily = useResource(useCallback((signal: AbortSignal) => getDaily(getPeriod(choice), signal), [choice]));
  const breakdown = useResource(useCallback((signal: AbortSignal) => getBreakdown(getPeriod(choice), level, signal), [choice, level]));
  function rowLabel(id: number | string | null) {
    if (id === null) return copy.screens.unknownGroup;
    return level === 'platform' ? platformName(String(id)) : copy.screens.entity(copy.screens.levels[level], id);
  }
  return <div className="page-stack">
    <h1>{copy.statistics.title}</h1><DemoNotice /><PeriodPicker value={choice} onChange={setChoice} />
    {summary.loading && <p role="status">{copy.screens.loading}</p>}
    {summary.error && <ErrorBanner message={summary.error} onRetry={summary.reload} />}
    {summary.data && <>
      <h2>{copy.screens.total}</h2><p className="muted">{copy.screens.dates(displayDate(summary.data.from), displayDate(summary.data.to))}</p>
      <SummaryFigures summary={summary.data} /><ClientsSummary summary={summary.data} />
    </>}
    <Card className="page-stack"><h2>{copy.statistics.chart}</h2>
      {daily.loading && <p role="status">{copy.screens.loading}</p>}
      {daily.error && <ErrorBanner message={daily.error} onRetry={daily.reload} />}
      {daily.data && <DailyChart daily={daily.data} />}
    </Card>
    <Card className="page-stack">
      <label className="stats-select">{copy.screens.level}<select value={level} onChange={(event) => setLevel(event.target.value as StatsLevel)}>
        {(Object.keys(copy.screens.levels) as StatsLevel[]).map((key) => <option key={key} value={key}>{copy.screens.levels[key]}</option>)}
      </select></label>
      {breakdown.loading && <p role="status">{copy.screens.loading}</p>}
      {breakdown.error && <ErrorBanner message={breakdown.error} onRetry={breakdown.reload} />}
      {breakdown.data && (breakdown.data.items.length ? <div className="stats-table-scroll" role="region" aria-label={copy.screens.levels[level]} tabIndex={0}>
        <table className="stats-table"><caption>{copy.screens.levels[level]}</caption>
          <thead><tr><th scope="col">{copy.screens.levels[level]}</th><th scope="col">{copy.home.spent}</th><th scope="col">{copy.ads.impressions}</th>
            <th scope="col">{copy.ads.clicks}</th><th scope="col">{copy.home.leads}</th><th scope="col">{copy.home.leadPrice}</th></tr></thead>
          <tbody>{breakdown.data.items.map((item) => <tr key={String(item.id)}><th scope="row">{rowLabel(item.id)}</th>
            <td><Money kopecks={item.cost_kop} /></td><td>{item.impressions.toLocaleString('ru-RU')}</td><td>{item.clicks.toLocaleString('ru-RU')}</td>
            <td>{item.leads.toLocaleString('ru-RU')}</td><td><NullableMoney value={item.cost_per_lead_kop} /></td></tr>)}</tbody>
        </table></div> : <EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} />)}
    </Card>
    <Sources sources={summary.data?.sources ?? []} />
  </div>;
}
