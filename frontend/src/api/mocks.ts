import { copy } from '../copy';
import { getPeriod, isoDate } from './period';
import type { Ad, AutopilotStatus, Breakdown, Daily, HistoryItem, Metrics, Period, StatsLevel, Summary } from './types';

const timestamp = new Date().toISOString();
const base = (cost: number, leads: number, impressions = 1200, clicks = 48): Metrics => ({
  cost_kop: cost, impressions, clicks, leads, ctr: impressions ? clicks / impressions : null,
  cost_per_click_kop: clicks ? Math.round(cost / clicks) : null,
  cost_per_lead_kop: leads ? Math.round(cost / leads) : null,
  clients: null, cost_per_client_kop: null, is_incomplete: false, incomplete_days: [],
  sources: [{ platform: 'direct', fetched_at: timestamp }],
});
let paused = false;
const history: HistoryItem[] = [{ id: 1, created_at: timestamp, actor: 'autopilot', reason_text: copy.screens.mockHistory, status: 'applied' }];
function record(reason_text: string) {
  history.unshift({ id: history[0].id + 1, created_at: new Date().toISOString(), actor: 'owner', reason_text, status: 'applied' });
}
const ads: Ad[] = [
  { id: 1, platform: 'direct', campaign_id: 1, campaign_name: copy.screens.mockCampaign,
    group_id: 1, group_name: copy.screens.mockGroup, title: copy.screens.mockAdTitle, text: copy.screens.mockAdText,
    image_url: null, status: 'active', reason: null, metrics: base(185000, 7) },
  { id: 2, platform: 'direct', campaign_id: 1, campaign_name: copy.screens.mockCampaign,
    group_id: 1, group_name: copy.screens.mockGroup, title: copy.screens.mockAdTitle, text: copy.screens.mockAdText,
    image_url: null, status: 'paused_auto', reason: copy.screens.mockReason, metrics: base(75000, 0, 900, 30) },
];
export function mockDaily(period: Period): Daily {
  const items: Daily['items'] = [];
  const last = new Date(`${period.to}T12:00:00`);
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - 2);
  for (const date = new Date(`${period.from}T12:00:00`); date <= last; date.setDate(date.getDate() + 1)) {
    const day = isoDate(date);
    const seed = date.getDate();
    const incomplete = day > isoDate(cutoff);
    items.push({ date: day, ...base(6000 + (seed % 7) * 1700, seed % 4, 200 + seed * 10, 8 + seed),
      is_incomplete: incomplete, incomplete_days: incomplete ? [day] : [] });
  }
  const incomplete_days = items.flatMap((item) => item.incomplete_days);
  return { business_id: 1, ...period, items, is_incomplete: incomplete_days.length > 0, incomplete_days };
}
function totals(period: Period): Metrics {
  const daily = mockDaily(period);
  const sums = daily.items.reduce((sum, day) => ({ cost: sum.cost + day.cost_kop, leads: sum.leads + day.leads,
    impressions: sum.impressions + day.impressions, clicks: sum.clicks + day.clicks }), { cost: 0, leads: 0, impressions: 0, clicks: 0 });
  return { ...base(sums.cost, sums.leads, sums.impressions, sums.clicks),
    is_incomplete: daily.is_incomplete, incomplete_days: daily.incomplete_days };
}
export function mockSummary(period: Period): Summary {
  const month = getPeriod('month');
  const spent = totals(month).cost_kop;
  const today = new Date();
  const weekStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  weekStart.setDate(weekStart.getDate() - (weekStart.getDay() + 6) % 7);
  const end = new Date(weekStart); end.setDate(end.getDate() - 1);
  const start = new Date(weekStart); start.setDate(start.getDate() - 28);
  return { business_id: 1, ...period, ...totals(period),
    clients_window: { from: isoDate(start), to: isoDate(end), clients: null, cost_kop: 0,
      cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4 },
    budget: { month: month.from, budget_kop: 1500000, spent_kop: spent, remaining_kop: Math.max(0, 1500000 - spent),
      days_remaining: new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate() - today.getDate() + 1,
      platforms: [{ platform: 'direct', spent_kop: spent, source: 'stats_daily', spent_at: null }] },
  };
}
export function mockBreakdown(period: Period, level: StatsLevel): Breakdown {
  const metrics = totals(period);
  return { business_id: 1, ...period, level, is_incomplete: metrics.is_incomplete,
    incomplete_days: metrics.incomplete_days, items: [{ id: level === 'platform' ? 'direct' : 1, ...metrics }] };
}
export function mockStatus(): AutopilotStatus {
  return { status: paused ? 'paused' : 'running', last_check_at: timestamp, auto_continue: true,
    needs_attention: [{ id: 'clients', title: copy.screens.mockAttention, description: copy.screens.mockAttentionHint, destination: 'clients' }] };
}
export function mockAds() { return { items: ads.map((ad) => ({ ...ad })) }; }
export function mockAdAction(id: number, action: 'pause' | 'resume') {
  const ad = ads.find((item) => item.id === id);
  if (!ad) throw new Error(copy.errors.notFound);
  ad.status = action === 'pause' ? 'paused_owner' : 'active'; ad.reason = null;
  record(action === 'pause' ? copy.screens.mockPaused(ad.title) : copy.screens.mockResumed(ad.title));
}
export function mockStopAll() {
  paused = true;
  ads.forEach((ad) => { if (ad.status === 'active' || ad.status === 'moderation') ad.status = 'paused_owner'; });
  record(copy.screens.stopped);
}
export function mockExperiments() {
  return { items: [{ id: 1, title: copy.screens.mockExperiment, comparing: copy.screens.mockComparing,
    remaining_impressions: 4200, status: 'running' as const, conclusion: null }] };
}
export function mockHistory() {
  return { items: history.slice(0, 3).map((item) => ({ ...item })), total: history.length, page: 1, page_size: 3 };
}
