// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { getPlan, launchPlan, saveAssets, type Plan } from '../api/plan';
import { PlanPage } from './PlanPage';

vi.mock('../api/plan', () => ({ getPlan: vi.fn(), launchPlan: vi.fn(), regenerateImages: vi.fn(), saveAssets: vi.fn(), uploadPhoto: vi.fn() }));

afterEach(() => { cleanup(); vi.mocked(getPlan).mockReset(); vi.mocked(launchPlan).mockReset(); });

const interrupted = 'Запуск рекламы не завершился. Всё, что он успел создать на площадках, мы остановили: деньги не тратятся.';
function plan(launch_error: string | null): Plan {
  return {
    id: 3, month_budget_kop: 3000000, direct_budget_kop: 3000000, vk_budget_kop: 0, generations_remaining: 2, medical_level: 'none',
    marking_confirmed: true, ready_to_launch: true, launch_error, status: 'draft',
    groups: [{ id: 1, name: 'Маникюр', platform: 'direct', audience: 'Рядом', daily_limit_kop: 100000, ads: [{ title: 'Маникюр', text: 'Запись онлайн' }],
      assets: [{ id: 1, url: '/api/plan/assets/1/file', kind: 'image', alt: 'Маникюр', selected: true }] }],
  };
}

it('после прерванного запуска план объясняет, что случилось с созданной рекламой', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(interrupted));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect((await screen.findByText(interrupted)).closest('[role="status"]')).not.toBeNull();
});

it('без сбоя запуска объяснения нет', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Маникюр', { selector: 'h2' });
  expect(screen.queryByText(interrupted)).toBeNull();
});

it('после новой неудачи запуска объяснение прошлой попытки убирается, остаётся новая ошибка', async () => {
  const failed = 'Запуск не удался: ВКонтакте отклонил запуск.';
  vi.mocked(getPlan).mockResolvedValue(plan(interrupted));
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(launchPlan).mockRejectedValue(new Error(failed));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText(interrupted);
  fireEvent.click(screen.getByRole('button', { name: 'Запустить рекламу' }));
  expect(await screen.findByText(failed)).toBeTruthy();
  expect(screen.queryByText(interrupted)).toBeNull();
  expect(launchPlan).toHaveBeenCalledWith(3, false);
});

it.each([
  'Реклама уже запущена по более новому плану. Обновите страницу.',
  'План устарел: сведения о бизнесе изменились, и мы собрали новый. Обновите страницу и проверьте новый план.',
])('запуск плана из старой вкладки показывает отказ и не объявляет рекламу запущенной: %s', async (refused) => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(launchPlan).mockRejectedValue(new Error(refused));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Запустить рекламу' }));
  expect(await screen.findByText(refused)).toBeTruthy();
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Ваш план');
  expect(screen.queryByText('Реклама запущена')).toBeNull();
  expect(screen.getByRole('button', { name: 'Запустить рекламу' })).toBeTruthy();
});
