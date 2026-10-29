import { useCallback, useEffect, useRef, useState } from 'react';
import { EmptyState, ErrorBanner, Money, PageHeader, PanelCard, PlatformBadge, SegmentedControl } from '../components';
import { copy } from '../copy';
import { getAds, getBreakdown, getDaily, getSummary } from '../api/screens';
import { getBusinessToday, getPeriod, type PeriodChoice } from '../api/period';
import { displayDate } from '../format';
import { useResource } from '../api/useResource';
import type { Daily, StatsLevel } from '../api/types';
import { ClientsSummary, DemoNotice, NullableMoney, PeriodPicker, Sources, SummaryFigures, adLabels } from './StatsShared';
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
  return <div className="stats-chart-block">
    <div className="stats-chart-key"><span className="stats-chart-key__spend">{copy.screens.chartSpend}</span><span className="stats-chart-key__leads">{copy.screens.chartLeads}</span></div>
    <p className="muted">{[copy.screens.chartExplanation, currentDay && copy.screens.chartToday, points.some((point) => point.is_incomplete) && copy.screens.chartPending].filter(Boolean).join(' ')}</p>
    <svg ref={chart} className="stats-chart" viewBox={`0 0 ${width} 260`} role="img" aria-label={copy.statistics.chart}>
      <title>{copy.statistics.chart}</title>
      {[...new Set([0, Math.ceil(maxLeads / 2), maxLeads])].map((leads) => {
        const fraction = leads / maxLeads;
        return <g key={leads}>
          <line className="stats-chart__grid" x1={left} x2={right} y1={y(fraction, 1)} y2={y(fraction, 1)} />
          <text className="stats-chart__tick" x={left - 8} y={y(fraction, 1) + 4} textAnchor="end">{formatMoney(Math.round(maxCost * fraction))}</text>
          <text className="stats-chart__tick" x={right + 8} y={y(fraction, 1) + 4}>{leads}</text>
        </g>;
      })}
      <text x="0" y="24">{copy.home.spent}</text><text x={width} y="24" textAnchor="end">{copy.screens.chartLeads}</text>
      <polyline className="stats-chart__line stats-chart__line--spend" points={line('cost_kop', maxCost)} />
      <polyline className="stats-chart__line stats-chart__line--leads" points={line('leads', maxLeads)} />
      {points.map((point, index) => <g key={point.date} className={point.is_incomplete ? 'stats-chart__dot stats-chart__dot--pending' : 'stats-chart__dot'}>
        <circle className="stats-chart__dot--spend" cx={x(index)} cy={y(point.cost_kop, maxCost)} r={point.is_incomplete ? 4 : 3} />
        <circle className="stats-chart__dot--leads" cx={x(index)} cy={y(point.leads, maxLeads)} r={point.is_incomplete ? 4 : 3} />
      </g>)}
      <text className="stats-chart__tick" x={left} y="242">{displayDate(points[0].date)}</text><text className="stats-chart__tick" x={right} y="242" textAnchor="end">{displayDate(points[points.length - 1].date)}</text>
    </svg>
    <details className="stats-chart-details"><summary>{copy.screens.chartDetails}</summary>
      <div className="table-scroll" tabIndex={0} role="region" aria-label={copy.screens.chartDetails}><table className="table stats-table">
        <thead><tr><th scope="col">{copy.screens.day}</th><th scope="col">{copy.home.spent}</th><th scope="col">{copy.home.leads}</th></tr></thead>
        <tbody>{daily.items.map((point) => <tr key={point.date}><th scope="row" className="mono">{displayDate(point.date)}{point.is_incomplete ? ' *' : ''}</th>
          <td className="num"><Money kopecks={point.cost_kop} /></td><td className="num">{point.leads}</td></tr>)}</tbody>
      </table></div>
      <p className="muted">{copy.screens.chartFootnote}</p>
    </details>
  </div>;
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
    if (level === 'platform') return <PlatformBadge platform={String(id)} />;
    return names.get(`${level}:${id}`) ?? copy.screens.entity(copy.screens.rowLabels[level], id);
  }
  const columns = [copy.home.spent, copy.ads.impressions, copy.ads.clicks, copy.home.leads, copy.home.leadPrice];
  return <div className="page-stack">
    <PageHeader title={copy.statistics.title} actions={<PeriodPicker value={choice} onChange={setChoice} />}
      description={summary.data && <span className="mono">{copy.screens.dates(displayDate(summary.data.from), displayDate(summary.data.to))}</span>} />
    <DemoNotice />
    {summary.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
    {summary.error && <ErrorBanner message={summary.error} onRetry={summary.reload} />}
    {summary.data && <section className="page-stack stats-total" aria-labelledby="stats-total">
      <h2 id="stats-total" className="visually-hidden">{copy.screens.total}</h2>
      <SummaryFigures summary={summary.data} />
    </section>}
    <PanelCard title={copy.statistics.chart} titleId="stats-chart">
      {daily.loading && <p className="loading" role="status">{copy.screens.loading}</p>}
      {daily.error && <ErrorBanner message={daily.error} onRetry={daily.reload} />}
      {daily.data && <DailyChart daily={daily.data.data} today={daily.data.today} />}
    </PanelCard>
    <PanelCard title={copy.screens.breakdown} titleId="stats-breakdown" flush className="stats-breakdown"
      action={<SegmentedControl label={copy.screens.level} value={level} onChange={setLevel}
        options={(Object.keys(copy.screens.levels) as StatsLevel[]).map((key) => ({ value: key, label: copy.screens.levels[key] }))} />}>
      {breakdown.loading && <p className="loading stats-panel-state" role="status">{copy.screens.loading}</p>}
      {breakdown.error && <div className="stats-panel-state"><ErrorBanner message={breakdown.error} onRetry={breakdown.reload} /></div>}
      {breakdown.data && (breakdown.data.items.length
        ? <div className="table-scroll" role="region" aria-labelledby="stats-breakdown" tabIndex={0}>
          <table className="table table--stack stats-table">
            <thead><tr><th scope="col">{copy.screens.rowLabels[level]}</th>{columns.map((name) => <th key={name} scope="col">{name}</th>)}</tr></thead>
            <tbody>{breakdown.data.items.map((item) => <tr key={String(item.id)}><th scope="row">{rowLabel(item.id)}</th>
              <td className="num" data-label={columns[0]}><Money kopecks={item.cost_kop} /></td>
              <td className="num" data-label={columns[1]}>{item.impressions.toLocaleString('ru-RU')}</td>
              <td className="num" data-label={columns[2]}>{item.clicks.toLocaleString('ru-RU')}</td>
              <td className="num" data-label={columns[3]}>{item.leads.toLocaleString('ru-RU')}</td>
              <td className="num" data-label={columns[4]}><NullableMoney value={item.cost_per_lead_kop} /></td></tr>)}</tbody>
          </table></div>
        : <EmptyState title={copy.screens.noStats} description={copy.screens.noStatsHint} />)}
    </PanelCard>
    {summary.data && <ClientsSummary summary={summary.data} />}
    <Sources sources={summary.data?.sources ?? []} />
  </div>;
}
