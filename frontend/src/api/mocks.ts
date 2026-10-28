import { copy } from '../copy';
import { ApiError } from './client';
import { isoDate, periodFor } from './period';
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
    group_id: 1, group_name: copy.screens.mockGroup, title: copy.screens.mockSecondAdTitle, text: copy.screens.mockSecondAdText,
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
  const month = periodFor('month');
  const spent = totals(month).cost_kop;
  const today = new Date();
  const weekStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  weekStart.setDate(weekStart.getDate() - (weekStart.getDay() + 6) % 7);
  const end = new Date(weekStart); end.setDate(end.getDate() - 1);
  const start = new Date(weekStart); start.setDate(start.getDate() - 28);
  const budget = mockMonthBudget();
  const clients = clientResults(period);
  const window = clientResults({ from: isoDate(start), to: isoDate(end) });
  return { business_id: 1, ...period, ...totals(period), clients: clients.clients, cost_per_client_kop: clients.cost_per_client_kop,
    clients_window: { from: isoDate(start), to: isoDate(end), ...window, unknown_weeks: 4 - window.answered_weeks },
    budget: { month: month.from, budget_kop: budget, spent_kop: spent + 15100, reported_kop: spent, remaining_kop: Math.max(0, budget - spent - 15100),
      days_remaining: new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate() - today.getDate() + 1,
      platforms: [{ platform: 'direct', spent_kop: spent + 15100, source: 'budget_ledger', spent_at: timestamp }] },
  };
}
export function mockBreakdown(period: Period, level: StatsLevel): Breakdown {
  const metrics = totals(period);
  return { business_id: 1, ...period, level, is_incomplete: metrics.is_incomplete,
    incomplete_days: metrics.incomplete_days, items: [{ id: level === 'platform' ? 'direct' : 1, ...metrics }] };
}
export function mockStatus(): AutopilotStatus {
  return { status: paused ? 'paused' : 'running', last_check_at: timestamp, auto_continue: true,
    needs_attention: questions.some((item) => !item.answered_at)
      ? [{ id: 'clients', title: copy.screens.mockAttention, description: copy.screens.mockAttentionHint, destination: 'clients' }] : [] };
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
  const text = 'Запись онлайн, мастер рядом с домом.';
  return { items: [
    { id: 2, title: copy.screens.mockImageExperiment, hypothesis: 'image' as const, comparing: copy.screens.mockImageComparing,
      arms: [1, 2].map((id) => ({ title: copy.screens.mockAdTitle, text, image: mockAsset(id).url })),
      remaining_impressions: 3100, status: 'running' as const, conclusion: null },
    { id: 1, title: copy.screens.mockExperiment, hypothesis: 'text' as const, comparing: copy.screens.mockComparing,
      arms: [{ title: copy.screens.mockAdTitle, text, image: null }, { title: copy.screens.mockSecondAdTitle, text, image: null }],
      remaining_impressions: 4200, status: 'running' as const, conclusion: null },
  ] };
}
export function mockHistory() {
  return { items: history.slice(0, 3).map((item) => ({ ...item })), total: history.length, page: 1, page_size: 3 };
}

// Onboarding and running screens share the business budget and account state.
import type { AccountCheck, BudgetHint, Business, BusinessInput, Description, Marking, Me, SiteCheck, StopResult } from './setup';
import type { Plan, PlanAsset } from './plan';
import type { HistoryEntry, HistoryFilter, HistoryPage } from './history';
import type { ClientQuestion, ClientQuestions } from './clients';
import { maskMarking } from './settings';
import type { BusinessSettings } from './settings';
import type { AppNotification } from './notifications';
interface SetupMockState {
  me: Omit<Me, 'has_business'>; business?: Business; description?: Description; counter?: number; goals?: number[];
  marking?: { direct: boolean; vk: boolean }; plan?: Plan; signedOut?: boolean;
}
const setupMockKey = 'ad-autopilot-setup-mock';
function readSetupMock(): SetupMockState {
  try { const saved = sessionStorage.getItem(setupMockKey); if (saved) return JSON.parse(saved) as SetupMockState; } catch { /* Storage may be unavailable. */ }
  return { me: { id: 1, login: copy.launchFlow.mockLogin, email: null, accounts: [{ id: 1, business_id: 1, platform: 'direct',
    external_login: copy.launchFlow.mockLogin, status: 'ok', scopes: [] }] } };
}
let setupMock = readSetupMock();
function persistSetupMock() {
  try { sessionStorage.setItem(setupMockKey, JSON.stringify(setupMock)); } catch { /* In-memory demo remains usable. */ }
}
export function mockSetupMe(): Me {
  if (setupMock.signedOut) throw new ApiError(copy.errors.unauthorized, 401);
  // Разделы макета показывают готовый бизнес и до мастера.
  return { ...structuredClone(setupMock.me), has_business: true };
}
export function mockSetupConnect(platform: 'login' | 'direct' | 'vk') {
  setupMock.signedOut = false;
  if (platform !== 'login') {
    setupMock.me.accounts = setupMock.me.accounts.filter((item) => item.platform !== platform);
    setupMock.me.accounts.push({ id: platform === 'direct' ? 1 : 2, business_id: 1, platform,
      external_login: copy.launchFlow.mockLogin, status: 'ok', scopes: [] });
  }
  persistSetupMock();
}
export function mockSetupLogout() { setupMock.signedOut = true; persistSetupMock(); }
export function mockSetupDisconnect(id: number): StopResult {
  setupMock.me.accounts = setupMock.me.accounts.filter((item) => item.id !== id);
  setupMock.plan = undefined; persistSetupMock();
  return { stopped: true, message: 'Кабинет отключён. Рекламу на этой площадке перед отключением остановили.' };
}
export function mockSetupBusiness(body: BusinessInput): Business {
  setupMock.business = { ...body, id: 1 }; setupMock.description = undefined;
  setupMock.counter = undefined; setupMock.goals = undefined; setupMock.marking = undefined; setupMock.plan = undefined;
  persistSetupMock(); return structuredClone(setupMock.business);
}
export function mockSetupSite(): SiteCheck {
  const field = (value: string, question: string) => ({ value, quote: value || null, verified: !!value, question });
  return { status: setupMock.business?.site_url ? 'ready' : 'blocked', pages_read: 3, services_found: 2,
    counter_id: setupMock.business?.site_url ? 12345 : null, medical_level: 'none',
    description: {
      services: field(copy.launchFlow.mockServices, copy.launchFlow.servicesQuestion),
      prices: field(copy.launchFlow.mockPrices, copy.launchFlow.pricesQuestion),
      address: field(copy.launchFlow.mockAddress, copy.launchFlow.addressQuestion),
      difference: field('', copy.launchFlow.differenceQuestion),
    } };
}
export function mockSetupDescription(description: Description) { setupMock.description = description; setupMock.plan = undefined; persistSetupMock(); }
export function mockSetupCheck(): { items: AccountCheck[] } {
  const connected = (platform: 'direct' | 'vk') => setupMock.me.accounts.some((item) => item.platform === platform && item.status === 'ok');
  return { items: [
    { platform: 'direct', ok: connected('direct'), message: connected('direct') ? copy.launchFlow.mockDirectWorks : copy.launchFlow.mockDirectMissing },
    ...connected('vk') ? [{ platform: 'vk' as const, ok: true, message: copy.launchFlow.mockVkWorks }] : [],
  ] };
}
export function mockSetupCounters() { return { items: [{ id: 12345, name: copy.launchFlow.mockCounter }] }; }
export function mockSetupGoals(counter: number) { return { items: counter === 12345 ? [{ id: 1, name: copy.launchFlow.mockGoal }] : [] }; }
export function mockSetupMetrica(counter: number, goals: number[]) { setupMock.counter = counter; setupMock.goals = goals; persistSetupMock(); }
export function mockSetupMarking(body: Marking) {
  // Never keep the INN, phone or manual account key in browser storage.
  setupMock.marking = body.confirmed_in_platform; setupMock.plan = undefined; persistSetupMock();
  businessSettings.marking = maskMarking(body);
}
function mockAsset(id: number, selected = false): PlanAsset {
  const color = id % 2 ? '#e7c2bb' : '#b8cfc3';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400" viewBox="0 0 600 400"><rect width="600" height="400" fill="${color}"/><circle cx="180" cy="180" r="110" fill="#fff" opacity=".4"/><path d="M320 340Q200 200 370 60Q520 190 420 340" fill="#245e4c" opacity=".5"/></svg>`;
  return { id, url: `data:image/svg+xml,${encodeURIComponent(svg)}`, kind: 'image', alt: copy.launchFlow.mockImage(id), label: copy.launchFlow.mockImage(id), selected };
}
// Пороги демо-данных близки к серверным для месяца в 31 день; настоящие считает сервер.
const mockVkFrom = 4_220_000;
function mockPlatformDaily(amount: number, vk: boolean) {
  const daily = amount * 0.92 / 31 / 1.25;
  return vk ? { direct: Math.floor(daily * 0.7), vk: Math.floor(daily * 0.3) } : { direct: Math.floor(daily) };
}
export function mockBudgetHint(amount: number, vk: boolean): BudgetHint {
  const on = vk && amount >= mockVkFrom;
  return { minimum_kop: 190_000, low_kop: 1_000_000, daily_kop: mockPlatformDaily(amount, on),
    off: vk && !on ? [{ platform: 'vk', from_kop: mockVkFrom, min_daily_kop: 30_000 }] : [] };
}
export function mockSetupPlan(): Plan {
  const direct = setupMock.me.accounts.some((item) => item.platform === 'direct' && item.status === 'ok');
  const budget = mockMonthBudget();
  const hint = mockBudgetHint(budget, setupMock.me.accounts.some((item) => item.platform === 'vk' && item.status === 'ok'));
  const vk = hint.daily_kop.vk !== undefined;
  const vkBudget = vk ? Math.floor(budget * 0.3) : 0;
  const marking = !!setupMock.marking?.direct && (!vk || !!setupMock.marking?.vk);
  const ready = !!(setupMock.business && direct && setupMock.description && setupMock.counter && setupMock.goals?.length && marking);
  if (setupMock.plan?.month_budget_kop !== budget) setupMock.plan = undefined;
  if (!setupMock.plan) setupMock.plan = { id: 1, month_budget_kop: budget, direct_budget_kop: budget - vkBudget,
    vk_budget_kop: vkBudget, daily_kop: hint.daily_kop, off: hint.off, generations_remaining: 3, medical_level: mockSetupSite().medical_level,
    marking_confirmed: marking, ready_to_launch: ready, blockers: [], status: 'draft',
    groups: (vk ? ['direct', 'vk'] as const : ['direct'] as const).map((platform, index) => ({
      id: index + 1, name: copy.plan.groupExample, platform, audience: copy.launchFlow.mockAudience,
      daily_limit_kop: Math.floor((platform === 'vk' ? vkBudget : budget - vkBudget) / 31),
      ads: [{ title: copy.screens.mockAdTitle, text: copy.screens.mockAdText }],
      assets: [mockAsset(index * 100 + 1, true), mockAsset(index * 100 + 2)],
    })) };
  setupMock.plan.ready_to_launch = ready; setupMock.plan.marking_confirmed = marking;
  setupMock.plan.blockers = ready ? [] : [marking ? copy.launchFlow.accountsError : copy.launchFlow.markingError];
  return structuredClone(setupMock.plan);
}
export function mockPlanRegenerate(groupId: number): Plan {
  mockSetupPlan(); const plan = setupMock.plan!;
  if (plan.generations_remaining <= 0) throw new Error(copy.plan.generationLimit);
  const group = plan.groups.find((item) => item.id === groupId);
  if (!group) throw new Error(copy.errors.notFound);
  const id = Math.max(...plan.groups.flatMap((item) => item.assets.map((asset) => asset.id))) + 1;
  group.assets.push(mockAsset(id), mockAsset(id + 1)); plan.generations_remaining--; persistSetupMock(); return mockSetupPlan();
}
export async function mockPlanUpload(groupId: number, file: File): Promise<Plan> {
  const url = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error(copy.launchFlow.uploadError)); reader.readAsDataURL(file);
  });
  mockSetupPlan(); const group = setupMock.plan!.groups.find((item) => item.id === groupId);
  if (!group) throw new Error(copy.errors.notFound);
  group.assets.push({ id: Date.now(), url, kind: 'upload', alt: file.name, label: '', selected: true });
  persistSetupMock(); return mockSetupPlan();
}
export function mockPlanAssets(ids: number[]) {
  mockSetupPlan(); setupMock.plan!.groups.forEach((group) => group.assets.forEach((asset) => { asset.selected = ids.includes(asset.id); }));
  persistSetupMock();
}
export function mockPlanLaunch(acknowledged: boolean) {
  const plan = mockSetupPlan();
  if (!plan.ready_to_launch || (plan.medical_level !== 'none' && !acknowledged)
    || plan.groups.some((group) => !group.assets.some((asset) => asset.selected))) throw new Error(copy.launchFlow.notReady);
  setupMock.plan!.status = 'launched'; persistSetupMock();
}

// History, clients and settings demo state.
const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3600_000).toISOString();
const historyEntries: HistoryEntry[] = [
  { id: 3, business_id: 1, created_at: hoursAgo(2), actor: 'autopilot', kind: 'pause', platform: 'direct', entity_type: 'ad', entity_id: '1',
    reason_text: 'Объявление потратило втрое больше обычного и не принесло ни одного обращения.',
    evidence: { cost_kop: 450000, leads: 0, clicks: 120 }, status: 'applied', reverts_id: null, can_revert: true, revert_warning: null },
  { id: 2, business_id: 1, created_at: hoursAgo(30), actor: 'owner', kind: 'budget', platform: null, entity_type: 'business', entity_id: '1',
    reason_text: 'Вы увеличили бюджет на месяц.', evidence: { budget_kop: 3000000 }, status: 'applied', reverts_id: null, can_revert: true, revert_warning: null },
  { id: 1, business_id: 1, created_at: hoursAgo(55), actor: 'external', kind: 'text', platform: 'vk', entity_type: 'ad', entity_id: '2',
    reason_text: 'Площадка изменила текст объявления после проверки.', evidence: {}, status: 'applied', reverts_id: null, can_revert: false, revert_warning: null },
];
export function mockHistoryPage(filter: HistoryFilter, pageSize: number): HistoryPage {
  const items = historyEntries.filter((item) => (!filter.platform || item.platform === filter.platform)
    && (!filter.actor || item.actor === filter.actor)).map((item) => structuredClone(item));
  return { items: items.slice(0, pageSize), total: items.length, page: 1, page_size: pageSize };
}
export function mockRevert(id: number): HistoryEntry {
  const original = historyEntries.find((item) => item.id === id);
  if (!original || !original.can_revert) throw new Error(copy.errors.conflict);
  original.status = 'reverted'; original.can_revert = false;
  const entry: HistoryEntry = { ...original, id: historyEntries[0].id + 1, created_at: new Date().toISOString(), actor: 'owner', kind: 'revert',
    reason_text: 'Вы отменили изменение.', evidence: {}, status: 'applied', reverts_id: id, can_revert: false };
  historyEntries.unshift(entry); return structuredClone(entry);
}
const weekStart = (weeksAgo: number) => { const day = new Date(); day.setDate(day.getDate() - ((day.getDay() + 6) % 7) - 7 * weeksAgo); return isoDate(day); };
const weekEnd = (start: string) => { const day = new Date(`${start}T12:00:00`); day.setDate(day.getDate() + 6); return isoDate(day); };
const questions: ClientQuestion[] = [
  { id: weekStart(1), week_start: weekStart(1), week_end: weekEnd(weekStart(1)), leads_count: 8, clients_count: null, asked_at: timestamp, answered_at: null },
  { id: weekStart(2), week_start: weekStart(2), week_end: weekEnd(weekStart(2)), leads_count: 11, clients_count: 4, asked_at: timestamp, answered_at: timestamp },
  { id: weekStart(3), week_start: weekStart(3), week_end: weekEnd(weekStart(3)), leads_count: 6, clients_count: null, asked_at: timestamp, answered_at: timestamp },
];
function clientResults(period?: Period) {
  const answered = questions.filter((item) => item.answered_at && item.clients_count !== null
    && (!period || item.week_start >= period.from && item.week_end <= period.to));
  const cost = answered.reduce((sum, item) => sum + item.leads_count * 90000, 0);
  const clients = answered.reduce((sum, item) => sum + (item.clients_count ?? 0), 0);
  return { clients: answered.length ? clients : null, cost_kop: cost,
    cost_per_client_kop: clients ? Math.round(cost / clients) : null, answered_weeks: answered.length };
}
export function mockClientQuestions(): ClientQuestions {
  return { items: structuredClone(questions), cost_per_client_kop: clientResults().cost_per_client_kop };
}
export function mockClientAnswer(id: string, clients: number | null) {
  const question = questions.find((item) => item.id === id);
  if (!question) throw new Error(copy.errors.notFound);
  if (clients !== null && (clients < 0 || clients > question.leads_count)) throw new Error(copy.errors.validation);
  question.clients_count = clients; question.answered_at = new Date().toISOString();
}
let businessSettings: BusinessSettings = { id: 1, site_url: 'https://example.ru', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null,
  auto_continue: true, marking: null, metrica_counter_id: null, lead_goal_ids: [], description_approved: false, medical_level: 'none',
  description: { fields: Object.fromEntries((['services', 'prices', 'address', 'difference'] as const).map((key) => [key,
    { value: { services: copy.launchFlow.mockServices, prices: copy.launchFlow.mockPrices, address: copy.launchFlow.mockAddress, difference: '' }[key],
      quote: null, verified: true, question: '' }])) as Description } };
function mockMonthBudget() { return setupMock.business?.month_budget_kop ?? businessSettings.month_budget_kop; }
export function mockBusinessSettings(): BusinessSettings {
  return structuredClone({ ...businessSettings, ...setupMock.business,
    description: setupMock.description ? { fields: setupMock.description } : businessSettings.description,
    metrica_counter_id: setupMock.counter ?? null, lead_goal_ids: setupMock.goals ?? [], description_approved: !!setupMock.description });
}
export function mockSaveBudget(kopecks: number) {
  businessSettings.month_budget_kop = kopecks;
  if (setupMock.business) setupMock.business.month_budget_kop = kopecks;
  setupMock.plan = undefined; persistSetupMock();
}
export function mockSaveBusinessSettings(autoContinue: boolean, average: number | null) {
  businessSettings.auto_continue = autoContinue; businessSettings.avg_check_kop = average;
}
export function mockPauseAutopilot() { paused = true; }
export function mockResumeAutopilot() { paused = false; }
export function mockDeleteMyData(): StopResult {
  businessSettings = { ...businessSettings, marking: null }; mockSetupLogout();
  return { stopped: true, message: 'Данные удалены. Рекламу перед удалением остановили.' };
}

const notifications: AppNotification[] = [
  { id: 2, kind: 'moderation', created_at: hoursAgo(1), level: 'warning', title: 'Площадка отклонила объявление',
    text: 'ВКонтакте не пропустил объявление «Маникюр рядом с вами»: на картинке слишком много текста. Замените картинку в разделе «Реклама».' },
  { id: 1, kind: 'budget', created_at: hoursAgo(26), level: 'info', title: 'Потрачена половина бюджета',
    text: 'За месяц потратили 15 000 ₽ из 30 000 ₽. Остальное распределим на оставшиеся дни.' },
];
export function mockNotifications(): AppNotification[] { return structuredClone(notifications); }
export function mockReadNotification(id: number) {
  const index = notifications.findIndex((item) => item.id === id);
  if (index < 0) throw new Error(copy.errors.notFound);
  notifications.splice(index, 1);
}
