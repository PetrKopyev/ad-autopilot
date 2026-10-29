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
  getAds: vi.fn(async () => ({ items: [] })),
  getExperiments: async () => ({ items: [] }),
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [] })),
}));
vi.mock('../api/history', () => ({ getHistoryPage: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 5 })), revertChange: vi.fn() }));
vi.mock('../api/clients', () => ({ getClientQuestions: vi.fn(async () => ({ items: [], cost_per_client_kop: null })), answerClientQuestion: vi.fn() }));
vi.mock('../api/period', () => ({ getPeriod: async () => ({ from: '2026-10-01', to: '2026-10-27' }) }));
vi.mock('../api/notifications', () => ({ getNotifications: async () => [], readNotification: vi.fn() }));
vi.mock('../api/useBusinessNow', () => ({ useBusinessNow: () => Date.parse('2026-10-27T10:05:00Z') }));
vi.mock('../api/settings', () => ({ resumeAutopilot: vi.fn() }));
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

it('пишет темп расхода из бюджета месяца: за прошедшие 27 дней и по бюджету на 31 день', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: '2026-10-27T10:01:00Z', auto_continue: true, needs_attention: [] });
  await show(2_700_000, 2_700_000);
  expect(await screen.findByText(copy.home.onTrack)).toBeTruthy();
  expect(screen.getByText(copy.home.pace(formatMoney(100_000), formatMoney(96_774)), raw)).toBeTruthy();
});

it('без ряда по дням плитки показываются без графиков', async () => {
  await show(0, 0);
  expect(document.querySelector('.sparkline')).toBeNull();
  expect(getDaily).toHaveBeenCalled();
});

it('рисует графики обращений из ряда по дням', async () => {
  vi.mocked(getDaily).mockResolvedValueOnce({ business_id: 1, from: '2026-10-01', to: '2026-10-03', incomplete_days: [], is_incomplete: false,
    items: [1, 3, 2].map((leads, index) => ({ date: `2026-10-0${index + 1}`, ...metrics(90_000, leads) })) });
  await show(0, 0);
  expect(document.querySelectorAll('.sparkline')).toHaveLength(2);
});

it('объясняет, почему реклама не включилась после переключения на «Работает»', async () => {
  const warning = 'Реклама не включилась: при текущем бюджете площадкам не хватает минимума. Увеличьте бюджет в настройках.';
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'paused', last_check_at: null, auto_continue: true, needs_attention: [] });
  vi.mocked(resumeAutopilot).mockResolvedValue(warning);
  await show(0, 0);
  const mode = await screen.findByRole('group', { name: copy.autopilotMode.label });
  fireEvent.click(within(mode).getByRole('button', { name: copy.autopilotMode.working }));
  expect(await screen.findByText(warning)).toBeTruthy();
});

it('«Пауза» останавливает рекламу только после подтверждения', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [] });
  vi.mocked(stopAllAds).mockResolvedValue();
  await show(0, 0);
  const mode = await screen.findByRole('group', { name: copy.autopilotMode.label });
  fireEvent.click(within(mode).getByRole('button', { name: copy.autopilotMode.paused }));
  expect(stopAllAds).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: copy.screens.confirmStop }));
  expect(await screen.findByText(copy.screens.stopped)).toBeTruthy();
  expect(stopAllAds).toHaveBeenCalledTimes(1);
});

it('сводит объявления в кампании: статус по работающему, расход и цена обращения по сумме', async () => {
  const ad = (id: number, campaign_id: number, status: Ad['status'], cost: number, leads: number, reason: string | null = null): Ad => ({
    id, platform: 'direct', campaign_id, campaign_name: `Кампания ${campaign_id}`, group_id: 1, group_name: 'Группа', title: 'Заголовок', text: 'Текст',
    image_url: null, status, reason, metrics: metrics(cost, leads) });
  vi.mocked(getAds).mockResolvedValue({ items: [ad(1, 1, 'active', 300_000, 2), ad(2, 1, 'paused_auto', 100_000, 0, 'дорого'), ad(3, 2, 'paused_auto', 50_000, 0, 'ни одного обращения')] });
  await show(0, 0);
  const table = await screen.findByRole('table');
  const [first, second] = within(table).getAllByRole('row').slice(1);
  expect(within(first).getByText(copy.status.working)).toBeTruthy();
  expect(within(first).getByText(formatMoney(400_000), raw)).toBeTruthy();
  expect(within(first).getByText(formatMoney(200_000), raw)).toBeTruthy();
  expect(within(first).queryByText('дорого')).toBeNull();
  expect(within(second).getByText(copy.status.stoppedAuto)).toBeTruthy();
  expect(within(second).getByText('ни одного обращения')).toBeTruthy();
});

it('отменяет изменение из журнала только после подтверждения', async () => {
  const entry: HistoryEntry = { id: 7, business_id: 1, created_at: '2026-10-27T09:00:00Z', actor: 'autopilot', kind: 'budget', platform: 'direct',
    entity_type: 'campaign', entity_id: '1', reason_text: 'Обращение здесь дешевле всего.', evidence: { budget_kop: 105_000 }, status: 'applied',
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
  fireEvent.change(within(need).getByRole('spinbutton', { name: copy.clientsScreen.countLabel }), { target: { value: '30' } });
  expect(send.disabled).toBe(true);
  fireEvent.change(within(need).getByRole('spinbutton', { name: copy.clientsScreen.countLabel }), { target: { value: '9' } });
  fireEvent.click(send);
  await vi.waitFor(() => expect(answerClientQuestion).toHaveBeenCalledWith('w1', 9));
});
