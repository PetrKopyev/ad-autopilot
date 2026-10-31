// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getClientQuestions } from '../api/clients';
import { getAutopilotStatus, getSummary } from '../api/screens';
import type { ClientsWindow } from '../api/types';
import { ClientsPage } from './ClientsPage';
import { MemoryRouter } from 'react-router';

vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })),
  getSummary: vi.fn() }));
vi.mock('../api/clients', async (original) => ({ ...await original<typeof import('../api/clients')>(), getClientQuestions: vi.fn(), answerClientQuestion: vi.fn() }));
vi.mock('../api/period', async (original) => ({ ...await original<typeof import('../api/period')>(), getPeriod: async () => ({ from: '2026-10-01', to: '2026-10-31' }) }));

afterEach(cleanup);

/** Окно клиентов, как его отдаёт сводная статистика: одно на «Клиентов» и «Статистику». */
function summaryResponse(fields: Partial<ClientsWindow>) {
  return {
    business_id: 1, from: '2026-10-01', to: '2026-10-31', incomplete_days: [], cost_kop: 0, impressions: 0, clicks: 0, leads: 0,
    ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null, clients: null, cost_per_client_kop: null, is_incomplete: false, sources: [],
    clients_window: { from: '2026-09-28', to: '2026-10-25', clients: null, cost_kop: 0, cost_per_client_kop: null, answered_weeks: 0, unknown_weeks: 4, ...fields },
    budget: { month: '2026-10-01', budget_kop: 3000000, spent_kop: 0, reported_kop: 0, remaining_kop: 3000000, days_remaining: 5, platforms: [] },
  };
}
function summaryWindow(fields: Partial<ClientsWindow>) {
  vi.mocked(getSummary).mockResolvedValue(summaryResponse(fields));
}

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
  summaryWindow({});
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [] });
  render(<ClientsPage />);
  expect(await screen.findByText(copy.clientsScreen.nothingOpen)).toBeTruthy();
  expect(screen.getByText(copy.clientsScreen.nothingAnswered)).toBeTruthy();
  expect(screen.getByText(copy.clientsScreen.costPerClientEmpty)).toBeTruthy();
  expect(screen.getByText('—')).toBeTruthy();
});

it('цена клиента — из того же окна, что на «Статистике»: отвеченные недели считаются, «не знаю» не мешает', async () => {
  // Сервер вопросов держит цену на «появится после ваших ответов» (там порог строже), а статистика по тем же
  // ответам уже считает: плитка должна сойтись со «Статистикой», а не с вопросами.
  summaryWindow({ clients: 5631, cost_kop: 1_891_000, cost_per_client_kop: 336, answered_weeks: 2, unknown_weeks: 2 });
  const week = { asked_at: '2026-10-26T09:00:00+03:00', answered_at: '2026-10-28T10:00:00+03:00' };
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [
    { id: 'w4', week_start: '2026-10-19', week_end: '2026-10-25', leads_count: 4938, clients_count: 2469, ...week },
    { id: 'w3', week_start: '2026-10-12', week_end: '2026-10-18', leads_count: 4986, clients_count: null, ...week },
    { id: 'w2', week_start: '2026-10-05', week_end: '2026-10-11', leads_count: 5100, clients_count: 3162, ...week },
    { id: 'w1', week_start: '2026-09-28', week_end: '2026-10-04', leads_count: 4800, clients_count: null, ...week },
  ] });
  render(<ClientsPage />);
  // Строковая игла не нормализуется матчером, а текст элемента — да: сравниваем с обычным пробелом.
  expect(await screen.findByText('3,36 ₽')).toBeTruthy();
  expect(screen.getByText(copy.home.clientsHint(2, 4))).toBeTruthy();
  expect(screen.queryByText(copy.clientsScreen.costPerClientEmpty)).toBeNull();
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

it('после ответа плитка «Цена клиента» пересчитывается без перезагрузки: сводная и вопросы запрашиваются заново', async () => {
  // До ответа окно без цены; сервер пересчитывает окно после ответа — плитка должна сойтись со свежей сводной сразу.
  vi.mocked(getSummary).mockResolvedValueOnce(summaryResponse({}))
    .mockResolvedValueOnce(summaryResponse({ clients: 5631, cost_kop: 1_891_000, cost_per_client_kop: 336, answered_weeks: 1, unknown_weeks: 3 }));
  const question = { week_start: '2026-10-19', week_end: '2026-10-25', asked_at: '2026-10-26T09:00:00+03:00' };
  vi.mocked(getClientQuestions).mockResolvedValueOnce({ cost_per_client_kop: null, items: [
    { id: 'open', leads_count: 100, clients_count: null, answered_at: null, ...question }] })
    .mockResolvedValueOnce({ cost_per_client_kop: null, items: [
      { id: 'open', leads_count: 100, clients_count: 12, answered_at: '2026-10-28T10:00:00+03:00', ...question }] });
  render(<ClientsPage />);
  const field = await screen.findByRole('textbox', { name: copy.clientsScreen.countLabel });
  expect(screen.getByText(copy.clientsScreen.costPerClientEmpty)).toBeTruthy();
  fireEvent.change(field, { target: { value: '12' } });
  fireEvent.click(screen.getByRole('button', { name: copy.clientsScreen.send }));
  expect(await screen.findByText('3,36 ₽')).toBeTruthy();
  expect(screen.getByText(copy.home.clientsHint(1, 4))).toBeTruthy();
  expect(screen.getByText(copy.clientsScreen.answerLine(12, 100))).toBeTruthy();
  expect(vi.mocked(getSummary).mock.calls.length).toBe(2);
  expect(vi.mocked(getClientQuestions).mock.calls.length).toBe(2);
});

it('неверное число объясняется у поля: дробь, минус, больше обращений', async () => {
  const question = { week_start: '2026-10-19', week_end: '2026-10-25', asked_at: '2026-10-26T09:00:00+03:00' };
  vi.mocked(getClientQuestions).mockResolvedValue({ cost_per_client_kop: null, items: [{ id: 'open', leads_count: 40, clients_count: null, answered_at: null, ...question }] });
  render(<ClientsPage />);
  const field = await screen.findByRole('textbox', { name: copy.clientsScreen.countLabel });
  const send = screen.getByRole('button', { name: copy.clientsScreen.send }) as HTMLButtonElement;
  for (const [value, error] of [['4,5', copy.clientsScreen.wholeNumber], ['1,5', copy.clientsScreen.wholeNumber], ['abc', copy.clientsScreen.digitsOnly], ['-5', copy.clientsScreen.negative], ['99', copy.clientsScreen.tooMany(40)]]) {
    fireEvent.change(field, { target: { value } });
    expect(screen.getByText(error)).toBeTruthy();
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(send.disabled).toBe(true);
  }
  fireEvent.change(field, { target: { value: '12' } });
  expect(field.getAttribute('aria-invalid')).toBe('false');
  expect(send.disabled).toBe(false);
});
