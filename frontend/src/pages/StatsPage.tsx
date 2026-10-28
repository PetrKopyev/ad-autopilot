import { useCallback, useEffect, useRef, useState } from 'react';
import { Card, EmptyState, ErrorBanner, Money } from '../components';
import { copy } from '../copy';
import { getAds, getBreakdown, getDaily, getSummary } from '../api/screens';
import { getBusinessToday, getPeriod, type PeriodChoice } from '../api/period';
import { displayDate } from '../format';
import { useResource } from '../api/useResource';
import type { Daily, StatsLevel } from '../api/types';
import { ClientsSummary, DemoNotice, NullableMoney, PeriodPicker, Sources, SummaryFigures, adLabels, platformName } from './StatsShared';
import { formatMoney } from '../components/Money';

function DailyChart({ daily, today }: { daily: Daily; today: string }) {
  const chart = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(700);
  // Сегодняшний день ещё идёт, и его неполные цифры обрушили бы линии: на графике его нет, в таблице он помечен.
  // Прошлые дни, которые площадки ещё уточняют, остаются на графике пустыми точками.
  const currentDay = daily.items.length > 1 && daily.items.some((point) => point.date === today);
  const points = currentDay ? daily.items.filter((point) => point.date !== today) : daily.items;
  const hasData = daily.items.some((point) => point.sources.length > 0);
  useEffect(() => {
    if (!chart.current) return;
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setWidth(entry.contentRect.width); });
    observer.observe(chart.current);
    return () => observer.disconnect();
  }, [hasData]);
  const maxCost = Math.max(100, ...points.map((point) => point.cost_kop));
  const maxLeads = Math.max(1, ...points.map((point) => point.leads));
  const left = 88; const right = width - 40;
  const x = (index: number) => points.length === 1 ? (left + right) / 2 : left + index * (right - left) / (points.length - 1);
  const y = (value: number, max: number) => 210 - value / max * 160;
  const line = (metric: 'cost_kop' | 'leads', max: number) => points.map((point, index) => `${x(index)},${y(point[metric], max)}`).join(' ');
  if (!hasData) return <EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} />;
  return <>
    <div className="stats-chart-key"><span>{copy.screens.chartSpend}</span><span>{copy.screens.chartLeads}</span></div>
    <p className="muted">{[copy.screens.chartExplanation, currentDay && copy.screens.chartToday, points.some((point) => point.is_incomplete) && copy.screens.chartPending].filter(Boolean).join(' ')}</p>
    <svg ref={chart} className="stats-chart" viewBox={`0 0 ${width} 260`} role="img" aria-label={copy.statistics.chart}>
      <title>{copy.statistics.chart}</title>
      {[...new Set([0, Math.ceil(maxLeads / 2), maxLeads])].map((leads) => {
        const fraction = leads / maxLeads;
        return <g key={leads}>
          <line x1={left} x2={right} y1={y(fraction, 1)} y2={y(fraction, 1)} stroke="#dce2e9" />
          <text x={left - 8} y={y(fraction, 1) + 4} textAnchor="end">{formatMoney(Math.round(maxCost * fraction))}</text>
          <text x={right + 8} y={y(fraction, 1) + 4}>{leads}</text>
        </g>;
      })}
      <text x="0" y="24">{copy.home.spent}</text><text x={width} y="24" textAnchor="end">{copy.screens.chartLeads}</text>
      <polyline points={line('cost_kop', maxCost)} fill="none" stroke="#245e4c" strokeWidth="3" strokeLinejoin="round" />
      <polyline points={line('leads', maxLeads)} fill="none" stroke="#326bc0" strokeWidth="3" strokeLinejoin="round" strokeDasharray="7 4" />
      {points.map((point, index) => <g key={point.date}>
        <circle cx={x(index)} cy={y(point.cost_kop, maxCost)} r={point.is_incomplete ? 4 : 3} fill={point.is_incomplete ? '#fff' : '#245e4c'} stroke="#245e4c" strokeWidth="2" />
        <circle cx={x(index)} cy={y(point.leads, maxLeads)} r={point.is_incomplete ? 4 : 3} fill={point.is_incomplete ? '#fff' : '#326bc0'} stroke="#326bc0" strokeWidth="2" />
      </g>)}
      <text x={left} y="242">{displayDate(points[0].date)}</text><text x={right} y="242" textAnchor="end">{displayDate(points[points.length - 1].date)}</text>
    </svg>
    <details className="stats-chart-details"><summary>{copy.screens.chartDetails}</summary>
      <div className="stats-table-scroll" tabIndex={0} role="region" aria-label={copy.screens.chartDetails}><table className="stats-table">
        <thead><tr><th scope="col">{copy.screens.day}</th><th scope="col">{copy.home.spent}</th><th scope="col">{copy.home.leads}</th></tr></thead>
        <tbody>{daily.items.map((point) => <tr key={point.date}><th scope="row">{displayDate(point.date)}{point.is_incomplete ? ' *' : ''}</th>
          <td><Money kopecks={point.cost_kop} /></td><td>{point.leads}</td></tr>)}</tbody>
      </table></div>
      <p className="muted">{copy.screens.chartFootnote}</p>
    </details>
  </>;
}

export function StatsPage() {
  const [choice, setChoice] = useState<PeriodChoice>('month');
  const [level, setLevel] = useState<StatsLevel>('platform');
  const summary = useResource(useCallback(async (signal: AbortSignal) => getSummary(await getPeriod(choice, signal), signal), [choice]));
  const daily = useResource(useCallback(async (signal: AbortSignal) => {
    const [data, today] = await Promise.all([getPeriod(choice, signal).then((period) => getDaily(period, signal)), getBusinessToday(signal)]);
    return { data, today };
  }, [choice]));
  const breakdown = useResource(useCallback(async (signal: AbortSignal) => getBreakdown(await getPeriod(choice, signal), level, signal), [choice, level]));
  // Кампании, группы и объявления называем так же, как в разделе «Реклама», а не номерами.
  const ads = useResource(useCallback((signal: AbortSignal) => getAds(signal), []));
  const names = new Map<string, string>();
  for (const ad of ads.data?.items ?? []) {
    names.set(`campaign:${ad.campaign_id}`, ad.campaign_name);
    names.set(`group:${ad.group_id}`, ad.group_name);
  }
  for (const [id, name] of adLabels(ads.data?.items ?? [])) names.set(`ad:${id}`, name);
  function rowLabel(id: number | string | null) {
    if (id === null) return copy.screens.unknownGroup;
    if (level === 'platform') return platformName(String(id));
    return names.get(`${level}:${id}`) ?? copy.screens.entity(copy.screens.rowLabels[level], id);
  }
  return <div className="page-stack">
    <h1>{copy.statistics.title}</h1><DemoNotice /><PeriodPicker value={choice} onChange={setChoice} />
    {summary.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {summary.error && <ErrorBanner message={summary.error} onRetry={summary.reload} />}
    {summary.data && <>
      <h2>{copy.screens.total}</h2><p className="muted">{copy.screens.dates(displayDate(summary.data.from), displayDate(summary.data.to))}</p>
      <SummaryFigures summary={summary.data} /><ClientsSummary summary={summary.data} />
    </>}
    <Card className="page-stack"><h2>{copy.statistics.chart}</h2>
      {daily.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
      {daily.error && <ErrorBanner message={daily.error} onRetry={daily.reload} />}
      {daily.data && <DailyChart daily={daily.data.data} today={daily.data.today} />}
    </Card>
    <Card className="page-stack">
      <label className="stats-select">{copy.screens.level}<select value={level} onChange={(event) => setLevel(event.target.value as StatsLevel)}>
        {(Object.keys(copy.screens.levels) as StatsLevel[]).map((key) => <option key={key} value={key}>{copy.screens.levels[key]}</option>)}
      </select></label>
      {breakdown.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
      {breakdown.error && <ErrorBanner message={breakdown.error} onRetry={breakdown.reload} />}
      {breakdown.data && (breakdown.data.items.length ? <>
        <p className="muted stats-scroll-hint" id="stats-scroll-help">{copy.screens.scrollHint}</p>
        <div className="stats-table-scroll" role="region" aria-label={copy.screens.breakdown} aria-describedby="stats-scroll-help" tabIndex={0}>
        <table className="stats-table">
          <thead><tr><th scope="col">{copy.screens.rowLabels[level]}</th><th scope="col">{copy.home.spent}</th><th scope="col">{copy.ads.impressions}</th>
            <th scope="col">{copy.ads.clicks}</th><th scope="col">{copy.home.leads}</th><th scope="col">{copy.home.leadPrice}</th></tr></thead>
          <tbody>{breakdown.data.items.map((item) => <tr key={String(item.id)}><th scope="row">{rowLabel(item.id)}</th>
            <td><Money kopecks={item.cost_kop} /></td><td>{item.impressions.toLocaleString('ru-RU')}</td><td>{item.clicks.toLocaleString('ru-RU')}</td>
            <td>{item.leads.toLocaleString('ru-RU')}</td><td><NullableMoney value={item.cost_per_lead_kop} /></td></tr>)}</tbody>
        </table></div></> : <EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} />)}
    </Card>
    <Sources sources={summary.data?.sources ?? []} />
  </div>;
}
