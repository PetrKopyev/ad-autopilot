// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { getPlan, launchPlan, regenerateImages, saveAssets, uploadPhoto, type Plan } from '../api/plan';
import { PlanPage } from './PlanPage';

vi.mock('../api/plan', () => ({ getPlan: vi.fn(), launchPlan: vi.fn(), regenerateImages: vi.fn(), saveAssets: vi.fn(), uploadPhoto: vi.fn() }));

afterEach(() => { cleanup(); vi.mocked(getPlan).mockReset(); vi.mocked(launchPlan).mockReset(); vi.mocked(regenerateImages).mockReset(); vi.mocked(saveAssets).mockReset(); vi.mocked(uploadPhoto).mockReset(); });

const interrupted = 'Запуск рекламы не завершился. Всё, что он успел создать на площадках, мы остановили: деньги не тратятся.';
function plan(launch_error: string | null): Plan {
  return {
    id: 3, month_budget_kop: 3000000, direct_budget_kop: 3000000, vk_budget_kop: 0, daily_kop: { direct: 71225 }, off: [], generations_remaining: 2, medical_level: 'none',
    marking_confirmed: true, ready_to_launch: true, blockers: [], launch_error, status: 'draft',
    groups: [{ id: 1, name: 'Маникюр', platform: 'direct', audience: 'Рядом', daily_limit_kop: 100000, ads: [{ title: 'Маникюр', text: 'Запись онлайн' }],
      assets: [{ id: 1, url: '/api/plan/assets/1/file', kind: 'image', alt: 'Френч с золотой фольгой', label: 'Френч с золотой фольгой', selected: true }] }],
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

it('под сгенерированной картинкой — отметка «Иллюстрация» и её идея', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText('Иллюстрация: Френч с золотой фольгой', { selector: 'span' })).not.toBeNull();
});

function twoGroups(): Plan {
  const base = plan(null);
  return { ...base, groups: [
    { ...base.groups[0], id: 1, name: 'Маникюр' },
    { ...base.groups[0], id: 2, name: 'Педикюр', assets: [{ ...base.groups[0].assets[0], id: 2, url: '/api/plan/assets/2/file', label: 'Педикюр у моря' }] },
  ] };
}
function deferred() {
  let resolve!: (value: Plan) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<Plan>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}
function card(name: string) {
  return within(screen.getByRole('heading', { name, level: 2 }).closest('.card') as HTMLElement);
}
function withNewImage(source: Plan, group: number, id: number): Plan {
  return { ...source, groups: source.groups.map((item) => item.id === group ? { ...item, assets: [...item.assets, { ...item.assets[0], id, url: `/api/plan/assets/${id}/file`, label: `Новая ${id}`, selected: true }] } : item) };
}

it('варианты картинок просят в нескольких группах: запросы идут по очереди, у каждой группы свой признак', async () => {
  const first = deferred(); const second = deferred();
  vi.mocked(getPlan).mockResolvedValue(twoGroups());
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(regenerateImages).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Педикюр', { selector: 'h2' });
  fireEvent.click(card('Маникюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  const running = await card('Маникюр').findByRole('button', { name: 'Рисуем картинки…' });
  expect(running.getAttribute('aria-busy')).toBe('true');
  expect(card('Маникюр').getByRole('checkbox').matches(':disabled')).toBe(true);
  expect(card('Педикюр').getByRole('checkbox').matches(':disabled')).toBe(false);
  fireEvent.click(card('Педикюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  expect(card('Педикюр').getByRole('button', { name: 'В очереди…' })).toHaveProperty('disabled', true);
  expect(screen.getByRole('button', { name: 'Запустить рекламу' })).toHaveProperty('disabled', true);
  expect(regenerateImages).toHaveBeenCalledTimes(1);

  fireEvent.click(card('Педикюр').getByRole('checkbox'));
  first.resolve(withNewImage(twoGroups(), 1, 10));
  await card('Маникюр').findByText('Иллюстрация: Новая 10', { selector: 'span' });
  await waitFor(() => expect(regenerateImages).toHaveBeenLastCalledWith(2));
  expect(saveAssets).toHaveBeenLastCalledWith([1, 10]);
  expect(card('Педикюр').getByRole('checkbox')).toHaveProperty('checked', false);

  second.resolve(withNewImage(withNewImage(twoGroups(), 1, 10), 2, 11));
  await card('Педикюр').findByText('Иллюстрация: Новая 11', { selector: 'span' });
  expect(card('Педикюр').getAllByRole('checkbox').map((box) => (box as HTMLInputElement).checked)).toEqual([false, true]);
  expect(screen.getByRole('button', { name: 'Запустить рекламу' })).toHaveProperty('disabled', false);
});

it('ошибка одной группы не останавливает очередь', async () => {
  const first = deferred();
  vi.mocked(getPlan).mockResolvedValue(twoGroups());
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(regenerateImages).mockReturnValueOnce(first.promise).mockResolvedValueOnce(withNewImage(twoGroups(), 2, 11));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Педикюр', { selector: 'h2' });
  fireEvent.click(card('Маникюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  fireEvent.click(card('Педикюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  first.reject(new Error('Не удалось получить новые картинки.'));
  expect(await screen.findByText('Не удалось получить новые картинки.')).toBeTruthy();
  await card('Педикюр').findByText('Иллюстрация: Новая 11', { selector: 'span' });
  expect(regenerateImages).toHaveBeenCalledTimes(2);
});

it('ждущий запрос можно отменить: он не уходит на сервер, а выполняемый доработает', async () => {
  const first = deferred();
  vi.mocked(getPlan).mockResolvedValue(twoGroups());
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(regenerateImages).mockReturnValueOnce(first.promise);
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Педикюр', { selector: 'h2' });
  fireEvent.click(card('Маникюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  fireEvent.click(card('Педикюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  expect(card('Маникюр').queryByRole('button', { name: 'Отменить' })).toBeNull();
  fireEvent.click(card('Педикюр').getByRole('button', { name: 'Отменить' }));
  expect(card('Педикюр').getByRole('button', { name: 'Попросить ещё варианты' })).toHaveProperty('disabled', false);
  expect(card('Педикюр').queryByRole('button', { name: 'Отменить' })).toBeNull();

  first.resolve(withNewImage(twoGroups(), 1, 10));
  await card('Маникюр').findByText('Иллюстрация: Новая 10', { selector: 'span' });
  await waitFor(() => expect(card('Маникюр').getByRole('button', { name: 'Попросить ещё варианты' })).toBeTruthy());
  expect(regenerateImages).toHaveBeenCalledTimes(1);
  expect(regenerateImages).toHaveBeenCalledWith(1);
  expect(card('Педикюр').getByRole('button', { name: 'Попросить ещё варианты' })).toHaveProperty('disabled', false);
});

it('пока своё фото группы ждёт или загружается, загрузить ещё нельзя; остаток вариантов учитывает очередь', async () => {
  const generated = deferred(); const uploaded = deferred();
  vi.mocked(getPlan).mockResolvedValue(twoGroups());
  vi.mocked(saveAssets).mockResolvedValue(undefined);
  vi.mocked(regenerateImages).mockReturnValueOnce(generated.promise);
  vi.mocked(uploadPhoto).mockReturnValueOnce(uploaded.promise);
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Педикюр', { selector: 'h2' });
  expect(card('Педикюр').getByText('Можно попросить ещё 2 варианта картинок.')).toBeTruthy();
  fireEvent.click(card('Маникюр').getByRole('button', { name: 'Попросить ещё варианты' }));
  expect(card('Педикюр').getByText('Можно попросить ещё 1 вариант картинок.')).toBeTruthy();
  const input = card('Педикюр').getByLabelText('Загрузить своё фото') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File(['photo'], 'photo.jpg', { type: 'image/jpeg' })] } });
  expect(input.disabled).toBe(true);
  generated.resolve(withNewImage(twoGroups(), 1, 10));
  await waitFor(() => expect(uploadPhoto).toHaveBeenCalledTimes(1));
  expect((card('Педикюр').getByLabelText('Загрузить своё фото') as HTMLInputElement).disabled).toBe(true);
  uploaded.resolve(withNewImage(withNewImage(twoGroups(), 1, 10), 2, 11));
  await waitFor(() => expect((card('Педикюр').getByLabelText('Загрузить своё фото') as HTMLInputElement).disabled).toBe(false));
});

it('картинку можно скачать, и ссылка не переключает отметку', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  const link = await screen.findByRole('link', { name: 'Скачать' });
  expect(link.getAttribute('href')).toBe('/api/plan/assets/1/file');
  expect(link.getAttribute('download')).toBe('ad-image-1.jpg');
  link.addEventListener('click', (event) => event.preventDefault());
  fireEvent.click(link);
  expect(screen.getByRole('checkbox')).toHaveProperty('checked', true);
});

it('плашка перед запуском называет настоящие причины, а не маркировку, если она подтверждена', async () => {
  const reason = 'В каждой группе отметьте хотя бы одну картинку.';
  vi.mocked(getPlan).mockResolvedValue({ ...plan(null), ready_to_launch: false, blockers: [reason] });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText(reason, { selector: 'li' })).not.toBeNull();
  expect(screen.queryByText(/подтвердите маркировку/)).toBeNull();
});

it('без причин плашки перед запуском нет', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Маникюр', { selector: 'h2' });
  expect(screen.queryByText('Перед запуском осталось:')).toBeNull();
});

const plain = (text: string) => (_: string, element: Element | null) => element?.tagName === 'P' && element.textContent?.replace(/ /g, ' ') === text;

it('бюджет показан суммой на площадку, а выключенная из-за бюджета площадка — с суммой, с которой она подключится', async () => {
  vi.mocked(getPlan).mockResolvedValue({ ...plan(null), off: [{ platform: 'vk', from_kop: 4220000, min_daily_kop: 30000 }] });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText(plain('Яндекс Директ — 30 000 ₽ в месяц, около 712 ₽ в день.'))).toBeTruthy();
  expect(screen.getByText(plain('ВКонтакте выключен из-за бюджета: площадке нужно минимум 300 ₽ в день, это бюджет от 42 200 ₽ в месяц. '
    + 'Пока весь бюджет идёт в Яндекс Директ. Поднимите бюджет — и ВКонтакте появится в новом плане до запуска.'))).toBeTruthy();
  expect(screen.queryByText(/Дневной расход/)).toBeNull();
});
