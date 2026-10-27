// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getHistoryPage, type HistoryEntry } from '../api/history';
import { HistoryPage } from './HistoryPage';

vi.mock('../api/history', () => ({ getHistoryPage: vi.fn(), revertChange: vi.fn() }));
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
