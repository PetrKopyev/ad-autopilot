// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { ApiError } from '../api/client';
import { getBusinessToday } from '../api/period';
import { getAds, getExperiments } from '../api/screens';
import type { Ad, Experiment } from '../api/types';
import { AdsPage } from './AdsPage';

vi.mock('../api/screens', () => ({ mockEnabled: false, getAds: vi.fn(), getExperiments: vi.fn(), setAdStatus: vi.fn() }));
vi.mock('../api/period', async (original) => ({ ...await original<typeof import('../api/period')>(), getBusinessToday: vi.fn(async () => '2026-11-03') }));

afterEach(() => { cleanup(); vi.mocked(getAds).mockReset(); vi.mocked(getExperiments).mockReset(); vi.mocked(getBusinessToday).mockRestore(); });

const text = 'Запись онлайн, мастер рядом с домом.';
function experiment(hypothesis: Experiment['hypothesis'], arms: Experiment['arms']): Experiment {
  return { id: 1, title: 'Сравниваем', hypothesis, comparing: 'Меняем только картинку, текст объявления тот же. До конца проверки: 4 дня.',
    arms, remaining_impressions: 1200, status: 'running', conclusion: null };
}
async function show(hypothesis: Experiment['hypothesis'], arms: Experiment['arms'], ads: Ad[] = []) {
  vi.mocked(getAds).mockResolvedValue({ items: ads });
  vi.mocked(getExperiments).mockResolvedValue({ items: [experiment(hypothesis, arms)] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  return screen.findByRole('list');
}

it('варианты подписаны по порядку: первый — А, второй — Б', async () => {
  const list = await show('text', [{ title: 'Маникюр рядом с вами', text, image: null }, { title: 'Выберите время для маникюра', text, image: null }]);
  const [first, second] = within(list).getAllByRole('listitem');
  expect(first.textContent).toBe(`${copy.ads.arm('А')}«Маникюр рядом с вами». ${text}`);
  expect(second.textContent).toBe(`${copy.ads.arm('Б')}«Выберите время для маникюра». ${text}`);
  expect(screen.queryByText(copy.ads.armNoImage)).toBeNull();
});

it('показывает картинки вариантов под своими буквами', async () => {
  const list = await show('image', [{ title: 'Маникюр', text, image: '/api/plan/assets/1/file' }, { title: 'Маникюр', text, image: '/api/plan/assets/2/file' }]);
  const [first, second] = within(list).getAllByRole('listitem');
  expect(within(first).getByRole('img', { name: copy.ads.armImage('А') }).getAttribute('src')).toBe('/api/plan/assets/1/file');
  expect(within(second).getByRole('img', { name: copy.ads.armImage('Б') }).getAttribute('src')).toBe('/api/plan/assets/2/file');
  expect(screen.queryByText(copy.ads.armNoImage)).toBeNull();
});

it('о недоступной картинке говорит только в проверке картинок, а не по одинаковому тексту', async () => {
  await show('image', [{ title: 'Маникюр', text, image: '/api/plan/assets/1/file' }, { title: 'Маникюр', text, image: null }]);
  expect(screen.getAllByText(copy.ads.armNoImage)).toHaveLength(1);
  cleanup();
  await show('audience', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }]);
  expect(screen.queryByText(copy.ads.armNoImage)).toBeNull();
});

it('строку «Что сравниваем» выводит как есть и варианты в ней не повторяет', async () => {
  await show('image', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }]);
  expect(screen.getByText(experiment('image', []).comparing)).toBeTruthy();
  expect(screen.getAllByText(`«Маникюр». ${text}`)).toHaveLength(2);
});

const ad: Ad = { id: 7, platform: 'direct', campaign_id: 1, campaign_name: 'Кампания', group_id: 1, group_name: 'Группа', title: 'Маникюр', text,
    image_url: null, status: 'paused_auto', reason: 'Потратили 211 ₽, получили 3 обращения.', metrics: {
      cost_kop: 0, impressions: 0, clicks: 0, leads: 0, ctr: null, cost_per_click_kop: null, cost_per_lead_kop: null,
      clients: null, cost_per_client_kop: null, is_incomplete: false, incomplete_days: [], sources: [] } };

it('расход объявлений подписан периодом, который запрошен у сервера', async () => {
  await show('text', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }], [ad]);
  expect(vi.mocked(getAds).mock.calls[0][1]).toEqual({ from: '2026-10-21', to: '2026-11-03' });
  expect(screen.getByText(copy.screens.adPeriodStopped(copy.screens.dates('21.10.2026', '03.11.2026')))).toBeTruthy();
});

it('демо-сервер не назвал дату — объявления за период по умолчанию сервера, без ошибки на странице', async () => {
  vi.mocked(getBusinessToday).mockRejectedValueOnce(new ApiError('Сервис временно недоступен.', 503));
  await show('text', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }], [ad]);
  expect(vi.mocked(getAds).mock.calls[0][1]).toBeUndefined();
  expect(screen.getByText('Цифры за последние 14 дней. В причине остановки — цифры на момент решения.')).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
});
