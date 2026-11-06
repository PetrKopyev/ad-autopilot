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
    entity_type: 'campaign', entity_id: '1', reason_code: null, reason_text: `Запись ${id}`, evidence: {}, status: 'applied',
    reverts_id: null, can_revert: true, revert_warning: null, turns_on: false,
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

it('отмена называет кампанию; отмена остановки одного объявления — «Вы вернули объявление»', async () => {
  const revert = (id: number, reverts_id: number, campaign: string | null) => ({ ...entry(id, 'revert'), reverts_id, campaign, can_revert: false });
  const ad = { ...entry(1, 'pause'), actor: 'autopilot' as const, entity_type: 'ad', campaign: 'Педикюр' };
  serve([revert(4, 1, 'Педикюр'), revert(5, 2, null), revert(6, 99, 'Маникюр'), ad, { ...entry(2, 'budget'), campaign: 'Маникюр' }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 4');
  const titles = screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);
  expect(titles).toEqual([
    labels.withCampaign(labels.resumedAd, 'Педикюр'), labels.revertedLine('изменение бюджета — «Маникюр»'),
    labels.withCampaign(labels.revertedPlain, 'Маникюр'), labels.withCampaign(labels.kinds.pause, 'Педикюр'),
    labels.withCampaign(labels.kinds.budget, 'Маникюр'),
  ]);
});

it('кнопки отката нет, если сервер не разрешает откат (кампания прежнего плана, защита бюджета)', async () => {
  serve([{ ...entry(1, 'pause'), can_revert: false }, { ...entry(2, 'budget'), can_revert: false }, entry(3, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.queryByRole('button', { name: labels.resume })).toBeNull();
  expect(screen.getAllByRole('button', { name: copy.history.undo })).toHaveLength(1);
});

it('защищённая запись защиты бюджета объясняет, почему её нельзя отменить', async () => {
  serve([{ ...entry(1, 'pause'), actor: 'system', can_revert: false, reason_code: 'BUDGET_REBALANCE' },
    { ...entry(2, 'pause'), actor: 'system', can_revert: false, reason_code: 'NO_ACTIVE_ADS' }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByText(labels.noRevertBudget)).toBeTruthy();
  // Не любая системная остановка — защита бюджета: без кода защиты пояснения нет.
  expect(screen.queryByText(labels.noRevertRevert)).toBeNull();
  expect(screen.getAllByText(labels.noRevertBudget)).toHaveLength(1);
});

it('запись об отмене изменения объясняет, что откатить её нельзя', async () => {
  serve([{ ...entry(1, 'revert'), reverts_id: 9, can_revert: false }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByText(labels.noRevertRevert)).toBeTruthy();
  expect(screen.queryByText(labels.noRevertBudget)).toBeNull();
});

it('запуск и возобновление без кнопки отмены, с подсказкой, как остановить рекламу', async () => {
  // Какая запись включает рекламу, решает сервер (turns_on): своего списка кодов у фронта нет.
  serve([{ ...entry(1, 'launch'), can_revert: false, reason_code: 'LAUNCH', turns_on: true },
    { ...entry(2, 'resume'), can_revert: false, reason_code: 'MODERATION_SETTLE', turns_on: true },
    { ...entry(4, 'resume'), can_revert: false, reason_code: 'SERVICE_RETURNED' }, entry(3, 'budget')]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getAllByText(labels.noRevertTurnOn)).toHaveLength(2);
  expect(screen.getAllByRole('button', { name: copy.history.undo })).toHaveLength(1);
});

it('пояснение не мешает отмене: кнопка у откатываемой записи, пояснение — только у защищённой', async () => {
  serve([entry(1, 'budget'), { ...entry(2, 'pause'), actor: 'system', can_revert: false, reason_code: 'BUDGET_STOP' }]);
  render(<HistoryPage />);
  await screen.findByText('Запись 1');
  expect(screen.getByRole('button', { name: copy.history.undo })).toBeTruthy();
  expect(screen.getAllByText(labels.noRevertBudget)).toHaveLength(1);
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

it('запуск и остановка по кампаниям — с названием кампании, а не одинаковыми строками', async () => {
  serve([{ ...entry(1, 'launch'), campaign: 'Маникюр' }, { ...entry(2, 'launch'), campaign: 'Педикюр' }, { ...entry(3, 'pause'), campaign: 'Маникюр' }]);
  render(<HistoryPage />);
  expect(await screen.findByRole('heading', { name: 'Запуск рекламы — «Маникюр»' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Запуск рекламы — «Педикюр»' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Остановка рекламы — «Маникюр»' })).toBeTruthy();
});

it('отмена старта сравнения — «Вы отменили сравнение вариантов», а не запуск объявления', async () => {
  const start = { ...entry(1, 'launch'), evidence: { experiment_key: 'k' }, status: 'reverted' as const, can_revert: false };
  serve([{ ...entry(2, 'revert'), reverts_id: 1, can_revert: false }, start]);
  render(<HistoryPage />);
  expect(await screen.findByRole('heading', { name: labels.revertedExperiment })).toBeTruthy();
});
