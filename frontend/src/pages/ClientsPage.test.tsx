// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getClientQuestions } from '../api/clients';
import { ClientsPage } from './ClientsPage';
import { MemoryRouter } from 'react-router';
import { getAutopilotStatus } from '../api/screens';

vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })) }));
vi.mock('../api/clients', () => ({ getClientQuestions: vi.fn(), answerClientQuestion: vi.fn() }));

afterEach(cleanup);

it('большие числа пишет с разделителем тысяч', async () => {
  const question = { week_start: '2026-10-19', week_end: '2026-10-25', asked_at: '2026-10-26T09:00:00+03:00' };
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [
    { id: 'open', leads_count: 2973, clients_count: null, answered_at: null, ...question },
    { id: 'answered', leads_count: 1250, clients_count: 1100, answered_at: '2026-10-26T10:00:00+03:00', ...question },
  ] });
  render(<ClientsPage />);
  expect(await screen.findByText('Сколько из 2 973 обращений стали клиентами?')).toBeTruthy();
  expect(screen.getByText('Число от 0 до 2 973.')).toBeTruthy();
  expect(screen.getByText('Клиентов: 1 100 из 1 250')).toBeTruthy();
});

it('без открытых вопросов и ответов показывает пустые состояния и прочерк вместо цены клиента', async () => {
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [] });
  render(<ClientsPage />);
  expect(await screen.findByText(copy.clientsScreen.nothingOpen)).toBeTruthy();
  expect(screen.getByText(copy.clientsScreen.nothingAnswered)).toBeTruthy();
  expect(screen.getByText(copy.clientsScreen.costPerClientEmpty)).toBeTruthy();
  expect(screen.getByText('—')).toBeTruthy();
});

it('до запуска с готовым планом не ждёт вопросов, а ведёт в план', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [] });
  render(<MemoryRouter><ClientsPage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.prelaunch.planReady })).toBeTruthy();
  expect(screen.getByText(copy.prelaunch.clients)).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.prelaunch.openPlan }).getAttribute('href')).toBe('/plan');
  expect(screen.queryByText(copy.clientsScreen.nothingOpen)).toBeNull();
});
