// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getHistoryPage, revertChange, type HistoryEntry } from '../api/history';
import { HistoryPage } from './HistoryPage';
import { MemoryRouter } from 'react-router';
import { getAutopilotStatus } from '../api/screens';

vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })) }));
vi.mock('../api/history', async (original) => ({ ...await original<typeof import('../api/history')>(), getHistoryPage: vi.fn(), revertChange: vi.fn() }));
vi.mock('../api/useBusinessNow', () => ({ useBusinessNow: () => null }));

const labels = copy.historyScreen;

function entry(id: number, kind: HistoryEntry['kind']): HistoryEntry {
  return {
    id, business_id: 1, created_at: '2026-10-20T10:00:00+03:00', actor: 'owner', kind, platform: 'direct',
    entity_type: 'campaign', entity_id: '1', reason_text: `Запись ${id}`, evidence: {}, status: 'applied',
    reverts_id: null, can_revert: true, revert_warning: null,
  };
}
function serve(items: HistoryEntry[]) {
  vi.mocked(getHistoryPage).mockResolvedValue({ items, total: items.length, page: 1, page_size: 20 });
}

afterEach(() => { cleanup(); vi.mocked(getHistoryPage).mockReset(); });

it('остановку предлагает продолжить, а не отменить', async () => {
  serve([entry(1, 'pause'), entry(2, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getAllByRole('button', { name: labels.resume })).toHaveLength(1);
  expect(screen.getAllByRole('button', { name: copy.history.undo })).toHaveLength(1);
  fireEvent.click(screen.getByRole('button', { name: labels.resume }));
  expect(screen.getByRole('group', { name: labels.resumeTitle })).toBeTruthy();
  expect(screen.getByRole('button', { name: labels.resumeYes })).toBeTruthy();
});

it('подсказка о ежедневном пересчёте видна только при фильтре «Все»', async () => {
  serve([entry(1, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByText(labels.routineHint)).toBeTruthy();
  fireEvent.change(screen.getByLabelText(labels.filterAuthor), { target: { value: 'autopilot' } });
  expect(screen.queryByText(labels.routineHint)).toBeNull();
  fireEvent.change(screen.getByLabelText(labels.filterAuthor), { target: { value: '' } });
  expect(screen.getByText(labels.routineHint)).toBeTruthy();
});

it('отмену остановки называет продолжением рекламы, другие отмены — по исходному изменению', async () => {
  const revert = (id: number, reverts_id: number) => ({ ...entry(id, 'revert'), reverts_id, can_revert: false });
  serve([revert(4, 1), revert(5, 2), revert(6, 99), entry(1, 'pause'), entry(2, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 4');
  const titles = screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
  expect(titles.slice(0, 3)).toEqual([labels.resumedAfterStop, labels.revertedLine('изменение бюджета'), labels.revertedPlain]);
});

it('кнопки отката нет, если сервер не разрешает откат (кампания прежнего плана, защита бюджета)', async () => {
  serve([{ ...entry(1, 'pause'), can_revert: false }, { ...entry(2, 'budget'), can_revert: false }, entry(3, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.queryByRole('button', { name: labels.resume })).toBeNull();
  expect(screen.getAllByRole('button', { name: copy.history.undo })).toHaveLength(1);
});

it('цифры причины видны сразу, отменённая запись помечена «Отменено» и без кнопки', async () => {
  serve([{ ...entry(1, 'pause'), actor: 'autopilot', evidence: { cost_kop: 450000, leads: 0 } },
    { ...entry(2, 'budget'), status: 'reverted', can_revert: false }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByLabelText(labels.numbers).textContent?.replace(/\s/g, ' ')).toBe('Потрачено4 500 ₽Обращений0');
  expect(screen.getByText(labels.statuses.reverted)).toBeTruthy();
  expect(screen.getAllByRole('button', { name: /Продолжить|Отменить/ })).toHaveLength(1);
});

it('ошибку отката показывает в подтверждении, а не теряет', async () => {
  serve([entry(1, 'budget')]);
  vi.mocked(revertChange).mockRejectedValueOnce(new Error('Изменение уже отменено.'));
  render(<HistoryPage />);
  fireEvent.click(await screen.findByRole('button', { name: copy.history.undo }));
  fireEvent.click(screen.getByRole('button', { name: labels.confirmYes }));
  const group = screen.getByRole('group', { name: labels.confirmTitle });
  expect(await within(group).findByText('Изменение уже отменено.')).toBeTruthy();
});

it('до запуска с готовым планом пустая история ведёт в план', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  serve([]);
  render(<MemoryRouter><HistoryPage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.prelaunch.planReady })).toBeTruthy();
  expect(screen.getByText(copy.prelaunch.history)).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.prelaunch.openPlan }).getAttribute('href')).toBe('/plan');
});

it('старт сравнения вариантов назван сравнением, а не «Запуском рекламы»; обычный запуск — как раньше', async () => {
  serve([{ ...entry(1, 'launch'), evidence: { experiment_key: 'k' }, can_revert: false }, { ...entry(2, 'launch'), can_revert: false }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByRole('heading', { name: labels.experimentStart })).toBeTruthy();
  expect(screen.getByRole('heading', { name: labels.kinds.launch })).toBeTruthy();
});
