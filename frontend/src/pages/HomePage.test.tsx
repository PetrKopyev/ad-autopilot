// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getAutopilotStatus, getSummary } from '../api/screens';
import { resumeAutopilot } from '../api/settings';
import type { Summary } from '../api/types';
import { formatMoney } from '../components/Money';
import { HomePage } from './HomePage';

vi.mock('../api/screens', () => ({
  mockEnabled: false, getSummary: vi.fn(), stopAllAds: vi.fn(),
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [] })),
  getHistory: async () => ({ items: [], total: 0, page: 1, page_size: 3 }),
}));
vi.mock('../api/period', () => ({ getPeriod: async () => ({ from: '2026-10-01', to: '2026-10-27' }) }));
vi.mock('../api/notifications', () => ({ getNotifications: async () => [], readNotification: vi.fn() }));
vi.mock('../api/useBusinessNow', () => ({ useBusinessNow: () => null }));
vi.mock('../api/settings', () => ({ resumeAutopilot: vi.fn() }));
vi.mock('../components/DemoPanel', () => ({ DemoPanel: () => null }));

const raw = { normalizer: (text: string) => text };
afterEach(() => { cleanup(); vi.mocked(getSummary).mockReset(); });

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

it('объясняет, почему реклама не включилась после «Возобновить»', async () => {
  const warning = 'Реклама не включилась: при текущем бюджете площадкам не хватает минимума. Увеличьте бюджет в настройках.';
  vi.mocked(getAutopilotStatus).mockResolvedValue({ status: 'paused', last_check_at: null, auto_continue: true, needs_attention: [] });
  vi.mocked(resumeAutopilot).mockResolvedValue(warning);
  await show(0, 0);
  fireEvent.click(screen.getByRole('button', { name: copy.settings.resume }));
  expect(await screen.findByText(warning)).toBeTruthy();
});
