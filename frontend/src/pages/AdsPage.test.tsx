// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { ApiError } from '../api/client';
import { getBusinessToday } from '../api/period';
import { getAds, getAutopilotStatus, getExperiments } from '../api/screens';
import type { Ad, Experiment } from '../api/types';
import { AdsPage } from './AdsPage';

vi.mock('../api/screens', () => ({ mockEnabled: false, getAds: vi.fn(), getExperiments: vi.fn(), setAdStatus: vi.fn(),
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })) }));
vi.mock('../api/settings', () => ({ getBusiness: vi.fn(async () => ({ avg_check_kop: 150000 })) }));
vi.mock('../api/period', async (original) => ({ ...await original<typeof import('../api/period')>(), getBusinessToday: vi.fn(async () => '2026-11-03') }));

afterEach(() => { cleanup(); vi.mocked(getAds).mockReset(); vi.mocked(getExperiments).mockReset(); vi.mocked(getBusinessToday).mockRestore(); });

const text = 'Запись онлайн, мастер рядом с домом.';
function experiment(hypothesis: Experiment['hypothesis'], arms: Experiment['arms']): Experiment {
  return { id: 1, title: 'Сравниваем', hypothesis, comparing: 'Меняем только картинку, текст объявления тот же. До конца проверки: 4 дня.',
    arms, remaining_impressions: 1200, status: 'running', conclusion: null };
}
async function show(hypothesis: Experiment['hypothesis'], arms: Experiment['arms'], ads: Ad[] = []) {
  vi.mocked(getAds).mockResolvedValue({ items: ads, campaigns: [] });
  vi.mocked(getExperiments).mockResolvedValue({ items: [experiment(hypothesis, arms)] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  return within(screen.getByRole('region', { name: copy.ads.experiments })).findByRole('list');
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

it('остановленное вами объявление подписано только «Остановлено вами», без повтора причины', async () => {
  await show('text', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }], [{ ...ad, status: 'paused_owner', reason: null }]);
  expect(screen.getByText(copy.screens.adStates.paused_owner).textContent).toBe('Остановлено вами');
  expect(screen.queryByText(/Остановлено вами:/)).toBeNull();
  expect(screen.getByText(copy.screens.adPeriod(copy.screens.dates('21.10.2026', '03.11.2026')))).toBeTruthy();
});

it('период цифр подписан один раз на всю страницу, у объявления — кнопка «Включить»', async () => {
  const second: Ad = { ...ad, id: 8, title: 'Педикюр', status: 'active', reason: null };
  await show('text', [{ title: 'Маникюр', text, image: null }, { title: 'Маникюр', text, image: null }], [ad, second]);
  expect(screen.getAllByText(/^Цифры за /)).toHaveLength(1);
  expect(screen.getByRole('button', { name: copy.common.enable })).toBeTruthy();
  expect(screen.getByRole('button', { name: copy.common.stop })).toBeTruthy();
  expect(screen.getByText(copy.screens.because(copy.status.stoppedAuto, ad.reason!))).toBeTruthy();
});

it('до запуска с готовым планом ведёт в план, а не в настройку', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  vi.mocked(getAds).mockResolvedValue({ items: [], campaigns: [] });
  vi.mocked(getExperiments).mockResolvedValue({ items: [] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.prelaunch.planReady })).toBeTruthy();
  expect(screen.getByText(copy.prelaunch.ads)).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.prelaunch.openPlan }).getAttribute('href')).toBe('/plan');
  expect(screen.queryByText(copy.screens.noAdsHint)).toBeNull();
});

it('до запуска без плана ведёт в мастер, как раньше', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: false });
  vi.mocked(getAds).mockResolvedValue({ items: [], campaigns: [] });
  vi.mocked(getExperiments).mockResolvedValue({ items: [] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.screens.noAds })).toBeTruthy();
  expect(screen.getByRole('link', { name: copy.prelaunch.setup }).getAttribute('href')).toBe('/setup');
});

it('кампания остановлена — её включённые объявления не «Работает», переключать их нечего', async () => {
  const live: Ad = { ...ad, id: 8, status: 'active', reason: null };
  vi.mocked(getAds).mockResolvedValue({ items: [live], campaigns: [{ id: 1, platform: 'direct', name: 'Кампания', status: 'paused_owner', daily: [] }] });
  vi.mocked(getExperiments).mockResolvedValue({ items: [] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  expect(await screen.findByText(copy.ads.campaignStoppedLine)).toBeTruthy();
  expect(screen.getByText(copy.screens.because(copy.status.stoppedOwner, copy.ads.campaignStopped))).toBeTruthy();
  expect(screen.queryByText(copy.status.working)).toBeNull();
  expect((screen.getByRole('button', { name: copy.common.stop }) as HTMLButtonElement).disabled).toBe(true);
});

it('одинаковые объявления с разными картинками подписаны номером картинки', async () => {
  const image = (id: number, url: string): Ad => ({ ...ad, id, status: 'active', reason: null, image_url: url });
  vi.mocked(getAds).mockResolvedValue({ items: [image(1, '/a.png'), image(2, '/b.png')], campaigns: [] });
  vi.mocked(getExperiments).mockResolvedValue({ items: [] });
  render(<MemoryRouter><AdsPage /></MemoryRouter>);
  expect(await screen.findByRole('heading', { name: copy.screens.adImageVariant(`Маникюр — «${text}»`, 1) })).toBeTruthy();
  expect(screen.getByRole('heading', { name: copy.screens.adImageVariant(`Маникюр — «${text}»`, 2) })).toBeTruthy();
});
