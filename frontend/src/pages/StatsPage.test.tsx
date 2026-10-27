// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { ApiError } from '../api/client';
import { getDaily } from '../api/screens';
import { getBusinessToday } from '../api/period';
import type { Daily } from '../api/types';
import { displayDate } from '../format';
import { StatsPage } from './StatsPage';

vi.mock('../api/screens', () => {
  const never = () => new Promise<never>(() => {});
  return { mockEnabled: false, getDaily: vi.fn(), getSummary: never, getBreakdown: never, getAds: never };
});
vi.mock('../api/period', () => ({ getPeriod: async () => ({ from: '2026-10-25', to: '2026-10-27' }), getBusinessToday: vi.fn() }));

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.mocked(getDaily).mockReset(); vi.mocked(getBusinessToday).mockReset(); });

function day(date: string, incomplete = false): Daily['items'][number] {
  return { date, cost_kop: 50_000, impressions: 300, clicks: 12, leads: 2, ctr: 0.04, cost_per_click_kop: 4_167, cost_per_lead_kop: 25_000,
    clients: null, cost_per_client_kop: null, is_incomplete: incomplete, incomplete_days: incomplete ? [date] : [],
    sources: [{ platform: 'direct', fetched_at: '2026-10-27T09:00:00+03:00' }] };
}
async function chart(today: string, items: Daily['items']) {
  vi.mocked(getBusinessToday).mockResolvedValue(today);
  vi.mocked(getDaily).mockResolvedValue({ business_id: 1, from: items[0].date, to: items[items.length - 1].date, items,
    is_incomplete: items.some((item) => item.is_incomplete), incomplete_days: items.flatMap((item) => item.incomplete_days) });
  const { container } = render(<StatsPage />);
  const svg = (await screen.findByRole('img', { name: copy.statistics.chart })) as unknown as SVGSVGElement;
  return {
    points: svg.querySelectorAll('circle').length / 2,
    labels: [...svg.querySelectorAll('text')].slice(-2).map((text) => text.textContent),
    note: container.querySelector('.stats-chart-key + p')!.textContent!,
    rows: container.querySelectorAll('.stats-chart-details tbody tr').length,
  };
}

it('сегодняшний день не рисует на графике, но оставляет в таблице', async () => {
  const result = await chart('2026-10-27', [day('2026-10-25', true), day('2026-10-26', true), day('2026-10-27', true)]);
  expect(result.points).toBe(2);
  expect(result.labels).toEqual([displayDate('2026-10-25'), displayDate('2026-10-26')]);
  expect(result.rows).toBe(3);
  expect(result.note).toContain(copy.screens.chartToday);
  expect(result.note).toContain(copy.screens.chartPending);
});

it('прошлый неполный день остаётся на графике пустой точкой', async () => {
  const result = await chart('2026-10-28', [day('2026-10-25'), day('2026-10-26', true), day('2026-10-27', true)]);
  expect(result.points).toBe(3);
  expect(result.labels).toEqual([displayDate('2026-10-25'), displayDate('2026-10-27')]);
  expect(result.note).not.toContain(copy.screens.chartToday);
  expect(result.note).toContain(copy.screens.chartPending);
});

it('без уточняемых дней подписи об уточнении нет', async () => {
  const result = await chart('2026-10-30', [day('2026-10-25'), day('2026-10-26'), day('2026-10-27')]);
  expect(result.points).toBe(3);
  expect(result.note).toBe(copy.screens.chartExplanation);
});

it('подписи об уточнении нет, если неполным был только сегодняшний день', async () => {
  const result = await chart('2026-10-27', [day('2026-10-25'), day('2026-10-26'), day('2026-10-27', true)]);
  expect(result.points).toBe(2);
  expect(result.note).toContain(copy.screens.chartToday);
  expect(result.note).not.toContain(copy.screens.chartPending);
});

it('период из одного сегодняшнего дня не обрезает', async () => {
  const result = await chart('2026-10-27', [day('2026-10-27', true)]);
  expect(result.points).toBe(1);
  expect(result.note).not.toContain(copy.screens.chartToday);
});

it('отказ сервера по периоду показывает его текст', async () => {
  const detail = 'Период не может быть длиннее 400 дней.';
  vi.mocked(getBusinessToday).mockResolvedValue('2026-10-27');
  vi.mocked(getDaily).mockRejectedValue(new ApiError(detail, 422, detail));
  render(<StatsPage />);
  expect((await screen.findByRole('alert')).textContent).toContain(detail);
});
