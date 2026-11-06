// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getAds, getAutopilotStatus, getDaily, getSummary, stopAllAds } from '../api/screens';
import { getHistoryPage, revertChange, type HistoryEntry } from '../api/history';
import { answerClientQuestion, getClientQuestions } from '../api/clients';
import { resumeAutopilot } from '../api/settings';
import type { Ad, Metrics, Summary } from '../api/types';
import { formatMoney } from '../components/Money';
import { HomePage } from './HomePage';

vi.mock('../api/screens', () => ({
  mockEnabled: false, getSummary: vi.fn(), stopAllAds: vi.fn(),
  getDaily: vi.fn(async () => { throw new Error('нет ряда'); }),
  getAds: vi.fn(async () => ({ items: [], campaigns: [] })),
  getExperiments: async () => ({ items: [] }),
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })),
}));
vi.mock('../api/history', async (original) => ({ ...await original<typeof import('../api/history')>(), getHistoryPage: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 5 })), revertChange: vi.fn() }));
vi.mock('../api/clients', async (original) => ({ ...await original<typeof import('../api/clients')>(), getClientQuestions: vi.fn(async () => ({ items: [], cost_per_client_kop: null })), answerClientQuestion: vi.fn() }));
vi.mock('../api/period', async (original) => ({ ...await original<typeof import('../api/period')>(), getPeriod: async () => ({ from: '2026-10-01', to: '2026-10-27' }) }));
vi.mock('../api/notifications', () => ({ getNotifications: vi.fn(async () => []), readNotification: vi.fn() }));
vi.mock('../api/useBusinessNow', () => ({ useBusinessNow: () => Date.parse('2026-10-27T10:05:00Z') }));
vi.mock('../api/settings', () => ({ resumeAutopilot: vi.fn(), getBusiness: vi.fn(async () => ({ avg_check_kop: null })) }));
vi.mock('../components/DemoPanel', () => ({ DemoPanel: () => null }));

const raw = { normalizer: (text: string) => text };
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) { this.open = true; };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) { this.open = false; };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const metrics = (cost_kop: number, leads: number): Metrics => ({
  cost_kop, impressions: 1000, clicks: 40, leads, ctr: 0.04, cost_per_click_kop: null, cost_per_lead_kop: leads ? Math.round(cost_kop / leads) : null,
  clients: null, cost_per_client_kop: null, is_incomplete: false, incomplete_days: [], sources: [],
});

async function show(spent: number, reported: number) {
  vi.mocked(getSummary).mockResolvedValue({
    business_id: 1, from: '2026-10-01', to: '2026-10-27', incomplete_days: [], cost_kop: reported, impressions: 0, clicks: 0, leads: 0,
    ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null, clients: null, cost_per_client_kop: null, is_incomplete: false, sources: [],
    clients_window: { from: '2026-09-22', to: '2026-10-19', clients: null, cost_kop: 0, cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4 },
    budget: { month: '2026-10-01', budget_kop: 3_000_000, spent_kop: spent, reported_kop: reported, remaining_kop: 3_000_000 - spent, days_remaining: 5, platforms: [] },
  } satisfies Summary);
  render(<MemoryRouter><HomePage /></MemoryRouter>);
  await screen.findByRole('group', { name: copy.budget.label(formatMoney(spent), formatMoney(3_000_000)) });
}

it('объясняет, почему потрачено больше, чем в статистике', async () => {
  await show(2_035_600, 2_020_500);
  expect(screen.getByText(copy.budget.fresher(formatMoney(2_020_500)), raw)).toBeTruthy();
});

it('без расхождения пояснения нет', async () => {
  await show(2_020_500, 2_020_500);
  expect(screen.queryByText(copy.budget.fresher(formatMoney(2_020_500)), raw)).toBeNull();
});

it('пишет темп расхода из бюджета месяца: за прошедшие 27 дней и по бюджету на 31 день, как в плане: целые рубли вниз', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: '2026-10-27T10:01:00Z', auto_continue: true, needs_attention: [], has_plan: true });
  await show(2_700_000, 2_700_000);
  expect(await screen.findByText(copy.home.onTrack)).toBeTruthy();
  expect(screen.getByText(copy.home.pace(formatMoney(100_000), formatMoney(96_700)), raw)).toBeTruthy();
});

it('без ряда по дням плитки показываются без графиков', async () => {
  await show(0, 0);
  expect(document.querySelector('.sparkline')).toBeNull();
  expect(getDaily).toHaveBeenCalled();
});

it('цену обращения рисует, только если дней с обращениями хотя бы три, и соединяет их через дни без обращений', async () => {
  vi.mocked(getDaily).mockResolvedValueOnce({ business_id: 1, from: '2026-10-01', to: '2026-10-05', incomplete_days: [], is_incomplete: false,
    items: [1, 0, 2, 0, 1].map((leads, index) => ({ date: `2026-10-0${index + 1}`, ...metrics(90_000, leads) })) });
  await show(0, 0);
  const lines = [...document.querySelectorAll('.sparkline--info polyline')];
  expect(lines).toHaveLength(1);
  expect(lines[0].getAttribute('points')!.split(' ')).toHaveLength(3);
  cleanup();
  vi.mocked(getDaily).mockResolvedValueOnce({ business_id: 1, from: '2026-10-01', to: '2026-10-03', incomplete_days: [], is_incomplete: false,
    items: [1, 0, 2].map((leads, index) => ({ date: `2026-10-0${index + 1}`, ...metrics(90_000, leads) })) });
  await show(0, 0);
  expect(document.querySelector('.sparkline--info')).toBeNull();
});

it('рисует графики обращений из ряда по дням', async () => {
  vi.mocked(getDaily).mockResolvedValueOnce({ business_id: 1, from: '2026-10-01', to: '2026-10-03', incomplete_days: [], is_incomplete: false,
    items: [1, 3, 2].map((leads, index) => ({ date: `2026-10-0${index + 1}`, ...metrics(90_000, leads) })) });
  await show(0, 0);
  expect(document.querySelectorAll('.sparkline')).toHaveLength(2);
});

it('объясняет, почему реклама не включилась после «Продолжить рекламу»', async () => {
  const warning = 'Реклама не включилась: при текущем бюджете площадкам не хватает минимума. Увеличьте бюджет в настройках.';
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'paused', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  vi.mocked(resumeAutopilot).mockResolvedValue(warning);
  await show(0, 0);
  fireEvent.click(await screen.findByRole('button', { name: copy.home.resumeAll }));
  expect(resumeAutopilot).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: copy.home.resumeYes }));
  expect(screen.queryByRole('button', { name: copy.home.stopAll })).toBeNull();
  expect(await screen.findByText(warning)).toBeTruthy();
});

it('«Продолжить» включил не все кампании: у работающей рекламы кнопка остаётся рядом с «Остановить», с пояснением', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true, resume_pending: true });
  vi.mocked(resumeAutopilot).mockResolvedValue(null);
  await show(0, 0);
  expect(await screen.findByText(copy.home.resumeLeft)).toBeTruthy();
  expect(screen.getByRole('button', { name: copy.home.stopAll })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: copy.home.resumeAll }));
  fireEvent.click(screen.getByRole('button', { name: copy.home.resumeYes }));
  await vi.waitFor(() => expect(resumeAutopilot).toHaveBeenCalledTimes(1));
});

it('пишет, когда будут первые решения автопилота, пока реклама работает', async () => {
  const line = 'Первые решения автопилота — примерно через 7 дней. Срок зависит от бюджета.';
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true, first_decisions: line });
  await show(0, 0);
  expect(await screen.findByText(line)).toBeTruthy();
});

it('остановка всей рекламы — одна кнопка и только после подтверждения', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  vi.mocked(stopAllAds).mockResolvedValue();
  await show(0, 0);
  fireEvent.click(await screen.findByRole('button', { name: copy.home.stopAll }));
  expect(screen.queryByRole('group', { name: copy.autopilotMode.label })).toBeNull();
  expect(screen.queryByRole('button', { name: copy.home.resumeAll })).toBeNull();
  expect(stopAllAds).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: copy.screens.confirmStop }));
  expect(await screen.findByText(copy.screens.stopped)).toBeTruthy();
  expect(stopAllAds).toHaveBeenCalledTimes(1);
});

it('сводит объявления в кампании: статус по работающему, расход и цена обращения по сумме', async () => {
  const ad = (id: number, campaign_id: number, status: Ad['status'], cost: number, leads: number, reason: string | null = null): Ad => ({
    id, platform: 'direct', campaign_id, campaign_name: `Кампания ${campaign_id}`, group_id: 1, group_name: 'Группа', title: 'Заголовок', text: 'Текст',
    image_url: null, status, reason, metrics: metrics(cost, leads) });
  const daily = [300, 0, 500, 450].map((cost, index) => ({ date: `2026-10-2${index + 4}`, cost_kop: cost * 100, leads: 0, is_incomplete: index > 1 }));
  vi.mocked(getAds).mockResolvedValue({ items: [ad(1, 1, 'active', 300_000, 2), ad(2, 1, 'paused_auto', 100_000, 0, 'дорого'), ad(3, 2, 'paused_auto', 50_000, 0, 'ни одного обращения')],
    campaigns: [{ id: 1, platform: 'direct', name: 'Кампания 1', status: 'active', daily }, { id: 2, platform: 'direct', name: 'Кампания 2', status: 'active', daily: daily.map((day) => ({ ...day, cost_kop: 0 })) }] });
  await show(0, 0);
  const table = await screen.findByRole('table');
  const [first, second] = within(table).getAllByRole('row').slice(1);
  expect(within(first).getByText(copy.status.working)).toBeTruthy();
  expect(within(first).getByText(formatMoney(400_000), raw)).toBeTruthy();
  expect(within(first).getByText(formatMoney(200_000), raw)).toBeTruthy();
  expect(within(first).queryByText('дорого')).toBeNull();
  expect(within(second).getByText(copy.status.stoppedAuto)).toBeTruthy();
  expect(within(second).getByText('ни одного обращения')).toBeTruthy();
  // Расход за 14 дней — линией по дням кампании; уточняемые дни пунктиром.
  const trend = within(first).getByRole('img', { name: copy.home.trendLabel('Кампания 1') });
  expect(trend.querySelectorAll('polyline')).toHaveLength(2);
  expect(trend.querySelectorAll('polyline.sparkline__dashed')).toHaveLength(1);
});

it('кабинет отключили — кампании остановлены: строка кампании не «Работает», а главная не пишет «Всё идёт по плану»', async () => {
  const ad: Ad = { id: 1, platform: 'direct', campaign_id: 1, campaign_name: 'Кампания 1', group_id: 1, group_name: 'Группа', title: 'Заголовок', text: 'Текст',
    image_url: null, status: 'active', reason: null, metrics: metrics(0, 0) };
  vi.mocked(getAds).mockResolvedValue({ items: [ad], campaigns: [{ id: 1, platform: 'direct', name: 'Кампания 1', status: 'paused_owner', daily: [] }] });
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: '2026-10-27T10:01:00Z', auto_continue: true, has_plan: true,
    needs_attention: [{ id: 'platform', title: 'Ошибка площадки', description: 'Проверьте подключение кабинета.', destination: 'settings' }] });
  await show(0, 0);
  const row = within(await screen.findByRole('table')).getAllByRole('row')[1];
  expect(within(row).getByText(copy.status.stoppedOwner)).toBeTruthy();
  expect(within(row).queryByText(copy.status.working)).toBeNull();
  expect(await screen.findByText(copy.home.noShows)).toBeTruthy();
  expect(screen.queryByText(copy.home.onTrack)).toBeNull();
  expect(document.querySelector('.home-state--success')).toBeNull();
});

it('один из кабинетов отключён: главная называет его и не пишет «Всё идёт по плану»', async () => {
  const ad: Ad = { id: 1, platform: 'direct', campaign_id: 1, campaign_name: 'Кампания 1', group_id: 1, group_name: 'Группа', title: 'Заголовок', text: 'Текст',
    image_url: null, status: 'active', reason: null, metrics: metrics(0, 0) };
  vi.mocked(getAds).mockResolvedValue({ items: [ad], campaigns: [{ id: 1, platform: 'direct', name: 'Кампания 1', status: 'active', daily: [] }] });
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: '2026-10-27T10:01:00Z', auto_continue: true, has_plan: true,
    needs_attention: [{ id: 'revoked_vk', title: 'Кабинет ВКонтакте отключён', description: 'Реклама там остановлена.', destination: 'settings' }] });
  await show(0, 0);
  expect(await screen.findByText('Кабинет ВКонтакте отключён')).toBeTruthy();
  expect(screen.queryByText(copy.home.onTrack)).toBeNull();
});

it('одна услуга и два объявления: блок сравнений объясняет, почему автопилот молчит, и советует указать средний чек', async () => {
  const ad = (id: number): Ad => ({ id, platform: 'direct', campaign_id: 1, campaign_name: 'Кампания 1', group_id: 1, group_name: 'Маникюр', title: 'Маникюр',
    text: `Текст ${id}`, image_url: null, status: 'active', reason: null, metrics: metrics(0, 0) });
  vi.mocked(getAds).mockResolvedValue({ items: [ad(1), ad(2)], campaigns: [{ id: 1, platform: 'direct', name: 'Кампания 1', status: 'active', daily: [] }] });
  await show(0, 0);
  expect(await screen.findByText(copy.screens.fewAds(2, false))).toBeTruthy();
  expect(screen.queryByText(copy.screens.experimentHint)).toBeNull();
});

it('кампания без ряда по дням показывается без графика', async () => {
  const ad: Ad = { id: 1, platform: 'direct', campaign_id: 1, campaign_name: 'Кампания 1', group_id: 1, group_name: 'Группа', title: 'Заголовок', text: 'Текст',
    image_url: null, status: 'active', reason: null, metrics: metrics(0, 0) };
  vi.mocked(getAds).mockResolvedValue({ items: [ad], campaigns: [{ id: 1, platform: 'direct', name: 'Кампания 1', status: 'active', daily: [] }] });
  await show(0, 0);
  const table = await screen.findByRole('table');
  expect(within(table).getByText('Кампания 1')).toBeTruthy();
  expect(within(table).queryByRole('img', { name: copy.home.trendLabel('Кампания 1') })).toBeNull();
  expect(table.querySelector('.sparkline')).toBeNull();
});

it('отменяет изменение из журнала только после подтверждения', async () => {
  const entry: HistoryEntry = { id: 7, business_id: 1, created_at: '2026-10-27T09:00:00Z', actor: 'autopilot', kind: 'budget', platform: 'direct',
    entity_type: 'campaign', entity_id: '1', reason_code: null, reason_text: 'Обращение здесь дешевле всего.', evidence: { budget_kop: 105_000 }, status: 'applied',
    reverts_id: null, can_revert: true, revert_warning: 'Дневной бюджет вернётся к 820 ₽.' };
  vi.mocked(getHistoryPage).mockResolvedValue({ items: [entry], total: 1, page: 1, page_size: 5 });
  vi.mocked(revertChange).mockResolvedValue({ ...entry, id: 8, kind: 'revert', reverts_id: 7 });
  await show(0, 0);
  const journal = await screen.findByRole('region', { name: copy.home.journal });
  expect(await within(journal).findByText(entry.reason_text)).toBeTruthy();
  fireEvent.click(within(journal).getByRole('button', { name: copy.history.undo }));
  expect(revertChange).not.toHaveBeenCalled();
  const confirm = within(journal).getByRole('group', { name: copy.historyScreen.confirmTitle });
  expect(within(confirm).getByText(entry.revert_warning!)).toBeTruthy();
  fireEvent.click(within(confirm).getByRole('button', { name: copy.historyScreen.confirmYes }));
  await vi.waitFor(() => expect(revertChange).toHaveBeenCalledWith(7));
});

it('спрашивает о клиентах прямо на главной', async () => {
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [
    { id: 'w1', week_start: '2026-10-20', week_end: '2026-10-26', leads_count: 24, clients_count: null, asked_at: '2026-10-27T06:00:00Z', answered_at: null }] });
  vi.mocked(answerClientQuestion).mockResolvedValue();
  await show(0, 0);
  const need = await screen.findByRole('region', { name: copy.home.needYou });
  expect(within(need).getByText(copy.clients.question(24), { exact: false })).toBeTruthy();
  const send = within(need).getByRole('button', { name: copy.clientsScreen.send }) as HTMLButtonElement;
  const count = within(need).getByRole('textbox', { name: copy.clientsScreen.countLabel });
  fireEvent.change(count, { target: { value: '30' } });
  expect(send.disabled).toBe(true);
  expect(within(need).getByText(copy.clientsScreen.tooMany(24))).toBeTruthy();
  expect(count.getAttribute('aria-invalid')).toBe('true');
  fireEvent.change(count, { target: { value: '1,5' } });
  expect(within(need).getByText(copy.clientsScreen.wholeNumber)).toBeTruthy();
  fireEvent.change(count, { target: { value: '9' } });
  expect(within(need).queryByText(copy.clientsScreen.wholeNumber)).toBeNull();
  fireEvent.click(send);
  await vi.waitFor(() => expect(answerClientQuestion).toHaveBeenCalledWith('w1', 9));
});

it('до запуска нет остановки и переключателя: главное действие — открыть план, без нулей и источников', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, has_plan: true,
    needs_attention: [{ id: 'budget', title: copy.screens.draft, description: 'Бюджет месяца — 30 000 ₽.', destination: 'settings' }] });
  vi.mocked(getSummary).mockResolvedValue({
    business_id: 1, from: '2026-10-01', to: '2026-10-27', incomplete_days: [], cost_kop: 0, impressions: 0, clicks: 0, leads: 0,
    ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null, clients: null, cost_per_client_kop: null, is_incomplete: false, sources: [],
    clients_window: { from: '2026-09-22', to: '2026-10-19', clients: null, cost_kop: 0, cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4 },
    budget: { month: '2026-10-01', budget_kop: 3_000_000, spent_kop: 0, reported_kop: 0, remaining_kop: 3_000_000, days_remaining: 5, platforms: [] },
  } satisfies Summary);
  vi.mocked(getAds).mockResolvedValueOnce({ items: [], campaigns: [] });
  render(<MemoryRouter><HomePage /></MemoryRouter>);
  expect(await screen.findByText(copy.prelaunch.afterBudget(formatMoney(3_000_000)), raw)).toBeTruthy();
  expect(screen.queryByRole('button', { name: copy.home.stopAll })).toBeNull();
  expect(screen.queryByRole('group', { name: copy.autopilotMode.label })).toBeNull();
  expect(screen.queryByText(copy.screens.noSources, { exact: false })).toBeNull();
  expect(screen.queryByText(copy.home.leads)).toBeNull();
  const links = screen.getAllByRole('link', { name: copy.prelaunch.openPlan });
  expect(links.length).toBe(2);
  expect(links.every((link) => link.getAttribute('href') === '/plan')).toBe(true);
  expect(screen.getByText(copy.prelaunch.planHint)).toBeTruthy();
  expect(await screen.findByRole('heading', { name: copy.prelaunch.planReady })).toBeTruthy();
  expect(screen.getByText(copy.prelaunch.campaigns)).toBeTruthy();
});

it('до запуска без плана ведёт в мастер', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, has_plan: false, needs_attention: [] });
  vi.mocked(getAds).mockResolvedValueOnce({ items: [], campaigns: [] });
  render(<MemoryRouter><HomePage /></MemoryRouter>);
  expect((await screen.findAllByRole('link', { name: copy.prelaunch.setup }))[0].getAttribute('href')).toBe('/setup');
  expect(await screen.findByRole('heading', { name: copy.home.noCampaigns })).toBeTruthy();
  expect(screen.queryByRole('button', { name: copy.home.stopAll })).toBeNull();
});

it('после запуска остановка на месте', async () => {
  await show(0, 0);
  expect(await screen.findByRole('button', { name: copy.home.stopAll })).toBeTruthy();
  expect(screen.queryByRole('link', { name: copy.prelaunch.openPlan })).toBeNull();
});

it('до запуска неподтверждённая маркировка — блок «что сделать» с инструкцией и ссылкой в настройки', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true,
    marking: { confirmed: { direct: false, vk: false }, transmission: { direct: 'marking_manual_required', vk: null } } });
  vi.mocked(getSummary).mockResolvedValue({
    business_id: 1, from: '2026-10-01', to: '2026-10-27', incomplete_days: [], cost_kop: 0, impressions: 0, clicks: 0, leads: 0,
    ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null, clients: null, cost_per_client_kop: null, is_incomplete: false, sources: [],
    clients_window: { from: '2026-09-22', to: '2026-10-19', clients: null, cost_kop: 0, cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4 },
    budget: { month: '2026-10-01', budget_kop: 3_000_000, spent_kop: 0, reported_kop: 0, remaining_kop: 3_000_000, days_remaining: 5, platforms: [] },
  } satisfies Summary);
  render(<MemoryRouter><HomePage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.marking.todoTitle })).toBeTruthy();
  expect(screen.getByText(copy.marking.manualRequired(copy.setup.accounts.direct))).toBeTruthy();
  for (const step of copy.marking.manualSteps.direct) expect(screen.getByText(step)).toBeTruthy();
  expect(screen.getByText(copy.marking.unconfirmed(copy.setup.accounts.direct))).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.marking.openSettings }).getAttribute('href')).toBe('/settings#marking');
});

it('подтверждённая и переданная маркировка не отнимает место на главной', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true,
    marking: { confirmed: { direct: true, vk: false }, transmission: { direct: 'marking_transmitted', vk: null } } });
  vi.mocked(getSummary).mockResolvedValue({
    business_id: 1, from: '2026-10-01', to: '2026-10-27', incomplete_days: [], cost_kop: 0, impressions: 0, clicks: 0, leads: 0,
    ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null, clients: null, cost_per_client_kop: null, is_incomplete: false, sources: [],
    clients_window: { from: '2026-09-22', to: '2026-10-19', clients: null, cost_kop: 0, cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4 },
    budget: { month: '2026-10-01', budget_kop: 3_000_000, spent_kop: 0, reported_kop: 0, remaining_kop: 3_000_000, days_remaining: 5, platforms: [] },
  } satisfies Summary);
  render(<MemoryRouter><HomePage /></MemoryRouter>);
  await screen.findByText(copy.prelaunch.afterTitle);
  expect(screen.queryByText(copy.marking.todoTitle)).toBeNull();
});

it('дробные обращения в уведомлении показывает округлёнными', async () => {
  const { getNotifications } = await import('../api/notifications');
  vi.mocked(getNotifications).mockResolvedValue([{ id: 1, kind: 'BUDGET_OPPORTUNITY', created_at: '2026-10-27T09:00:00Z', level: 'info',
    title: 'Можно получить больше обращений',
    text: 'При увеличении дневного бюджета на 136,75 ₽ можно получить ещё примерно 90,6 обращения в день. Это оценка по текущей цене.' }]);
  await show(0, 0);
  expect(await screen.findByText(/можно получить ещё ≈91 обращение в день/)).toBeTruthy();
  expect(screen.queryByText(/90,6 обращения/)).toBeNull();
});
