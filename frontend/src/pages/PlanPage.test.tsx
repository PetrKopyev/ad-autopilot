// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getForecast, type Forecast } from '../api/forecast';
import { getPlan, launchPlan, regenerateImages, saveAssets, uploadPhoto, type Plan } from '../api/plan';
import { getBusiness, type BusinessSettings } from '../api/settings';
import { PlanPage } from './PlanPage';

vi.mock('../api/plan', async (original) => ({ ...await original<typeof import('../api/plan')>(), getPlan: vi.fn(), launchPlan: vi.fn(), regenerateImages: vi.fn(), saveAssets: vi.fn(), uploadPhoto: vi.fn() }));
vi.mock('../api/forecast', () => ({ getForecast: vi.fn() }));
vi.mock('../api/settings', () => ({ getBusiness: vi.fn() }));

beforeEach(() => {
  vi.mocked(getForecast).mockRejectedValue(new Error('Не найдено.'));
  vi.mocked(getBusiness).mockRejectedValue(new Error('Не найдено.'));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.mocked(getPlan).mockReset(); vi.mocked(launchPlan).mockReset(); vi.mocked(regenerateImages).mockReset(); vi.mocked(saveAssets).mockReset(); vi.mocked(uploadPhoto).mockReset(); vi.mocked(getForecast).mockReset(); vi.mocked(getBusiness).mockReset(); });

const interrupted = 'Запуск рекламы не завершился. Всё, что он успел создать на площадках, мы остановили: деньги не тратятся.';
function plan(launch_error: string | null): Plan {
  return {
    id: 3, month_budget_kop: 3000000, direct_budget_kop: 3000000, vk_budget_kop: 0, daily_kop: { direct: 71225 }, off: [], generations_remaining: 2, medical_level: 'none',
    marking_confirmed: true, ready_to_launch: true, blockers: [], launch_error, status: 'draft', direct_services_total: 1,
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
// Справа открыта одна группа: выбираем её в списке и ищем внутри её панели.
function card(name: string) {
  fireEvent.click(screen.getByRole('button', { name: new RegExp(name) }));
  return within(screen.getByRole('heading', { name, level: 2 }).closest('section') as HTMLElement);
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
  await screen.findByRole('button', { name: /Педикюр/ });
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
  await screen.findByRole('button', { name: /Педикюр/ });
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
  await screen.findByRole('button', { name: /Педикюр/ });
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
  await screen.findByRole('button', { name: /Педикюр/ });
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
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-10-28T12:00:00+03:00'));
  vi.mocked(getPlan).mockResolvedValue({ ...plan(null), off: [{ platform: 'vk', from_kop: 4850000, min_daily_kop: 30000 }] });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  // В день — бюджет площадки на дни месяца (30 000 ₽ / 31), а не дневной лимит кампании из daily_kop.
  expect(await screen.findByText(plain('Яндекс Директ — 30 000 ₽ в месяц, в среднем 968 ₽ в день.'))).toBeTruthy();
  expect(screen.getByText(plain('Площадка ВКонтакте выключена из-за бюджета: ей нужно минимум 300 ₽ в день, это бюджет от 48 500 ₽ в месяц. '
    + 'Пока весь бюджет идёт в Яндекс Директ. Поднимите бюджет — и площадка ВКонтакте появится в новом плане до запуска.'))).toBeTruthy();
  expect(screen.queryByText(/Дневной расход/)).toBeNull();
});

it('у запущенного плана нет оценки дневного лимита и обещания нового плана', async () => {
  vi.mocked(getPlan).mockResolvedValue({ ...plan(null), status: 'launched', daily_kop: {}, off: [{ platform: 'vk', from_kop: 4850000, min_daily_kop: 30000 }] });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText(plain('Яндекс Директ — 30 000 ₽ в месяц.'))).toBeTruthy();
  expect(screen.getByText(plain('Площадка ВКонтакте выключена из-за бюджета: ей нужно минимум 300 ₽ в день, это бюджет от 48 500 ₽ в месяц. '
    + 'Весь бюджет идёт в Яндекс Директ.'))).toBeTruthy();
  expect(screen.queryByText(/в день\./)).toBeNull();
});

it('у запущенного плана с несколькими площадками нет нулевых лимитов', async () => {
  vi.mocked(getPlan).mockResolvedValue({ ...plan(null), status: 'launched', direct_budget_kop: 2000000, vk_budget_kop: 1000000, daily_kop: {} });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText(plain('Яндекс Директ — 20 000 ₽ в месяц.'))).toBeTruthy();
  expect(screen.getByText(plain('ВКонтакте — 10 000 ₽ в месяц.'))).toBeTruthy();
  expect(screen.queryByText(/около|в день/)).toBeNull();
});

function forecast(fields: Partial<Forecast>): Forecast {
  return { status: 'ready', leads_min: 9, leads_max: 26, clicks_min: 450, clicks_max: 870, cpc_rub: 32, cpc_rub_max: 61, budget_rub: 26000,
    conversion: { min: 0.02, max: 0.03, source: 'benchmark', label: 'ориентир по рынку услуг, не измерение' },
    explanation: 'За месяц 450–870 кликов.', explanation_by_ai: false,
    sources: [{ what: 'Клики и цена клика', source: 'Яндекс Директ, прогноз бюджета', url: 'https://yandex.ru/dev/direct/' }],
    computed_at: '2026-10-29T10:00:00Z', demo: false, vk: null, ...fields };
}
const forecastPanel = () => within(screen.getByRole('heading', { name: 'Прогноз на месяц', level: 2 }).closest('section') as HTMLElement);

it('готовый прогноз: диапазон обращений с сервера, пояснение, источник и пометка, что прогноз может ошибаться', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValue(forecast({ vk: { status: 'unavailable', label: 'Для ВКонтакте прогноз появится после первых дней работы.' } }));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByRole('heading', { name: 'Прогноз на месяц' });
  expect(forecastPanel().getByText('≈ 9–26')).toBeTruthy();
  expect(forecastPanel().getByText('За месяц 450–870 кликов.')).toBeTruthy();
  expect(forecastPanel().getByText('Прогноз, может ошибаться.')).toBeTruthy();
  expect(forecastPanel().getByText('Для ВКонтакте прогноз появится после первых дней работы.')).toBeTruthy();
  expect(forecastPanel().getByRole('link', { name: 'Яндекс Директ, прогноз бюджета' }).getAttribute('href')).toBe('https://yandex.ru/dev/direct/');
  expect(within(screen.getByRole('region', { name: 'Итог плана' })).getByText('≈ 9–26')).toBeTruthy();
  expect(screen.queryByText('демо')).toBeNull();
});

it('пояснение от ИИ уже с пометкой — вторую не добавляем; демо-ответ помечен', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValue(forecast({ explanation: 'Пересказ. Пояснение написал ИИ — это прогноз, он может ошибаться.', explanation_by_ai: true, demo: true }));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByRole('heading', { name: 'Прогноз на месяц' });
  expect(screen.queryByText('Прогноз, может ошибаться.')).toBeNull();
  expect(forecastPanel().getByText('демо')).toBeTruthy();
});

it('недоступный прогноз — объяснение сервера без чисел', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValue(forecast({ status: 'unavailable', leads_min: null, leads_max: null, explanation: 'Прогноз появится, когда Яндекс одобрит доступ.', sources: [] }));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText('Прогноз появится, когда Яндекс одобрит доступ.')).toBeTruthy();
  expect(screen.queryByText(/≈/)).toBeNull();
});

it('пока прогноз считается, спрашиваем снова, а после двух минут просим обновить страницу позже', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValue(forecast({ status: 'pending', leads_min: null, leads_max: null, explanation: '', sources: [] }));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await vi.waitFor(() => expect(screen.getByText('Считаем прогноз…')).toBeTruthy());
  await vi.advanceTimersByTimeAsync(5000);
  expect(getForecast).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(120_000);
  expect(screen.getByText('Яндекс долго готовит прогноз, обновите страницу позже.')).toBeTruthy();
  const calls = vi.mocked(getForecast).mock.calls.length;
  await vi.advanceTimersByTimeAsync(30_000);
  expect(getForecast).toHaveBeenCalledTimes(calls);
});

it('после готового прогноза больше не спрашиваем', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValueOnce(forecast({ status: 'pending', leads_min: null, leads_max: null, explanation: '', sources: [] }))
    .mockResolvedValue(forecast({}));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await vi.waitFor(() => expect(screen.getByText('Считаем прогноз…')).toBeTruthy());
  await vi.advanceTimersByTimeAsync(5000);
  expect(forecastPanel().getByText('≈ 9–26')).toBeTruthy();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(getForecast).toHaveBeenCalledTimes(2);
});

it('уйдя со страницы, прогноз больше не спрашиваем', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValue(forecast({ status: 'pending', leads_min: null, leads_max: null, explanation: '', sources: [] }));
  const { unmount } = render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await vi.waitFor(() => expect(screen.getByText('Считаем прогноз…')).toBeTruthy());
  unmount();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(getForecast).toHaveBeenCalledTimes(1);
  expect(vi.mocked(getForecast).mock.calls[0][0]?.aborted).toBe(true);
});

it('сбой сети во время опроса убирает блок прогноза и останавливает опрос', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  vi.mocked(getForecast).mockResolvedValueOnce(forecast({ status: 'pending', leads_min: null, leads_max: null, explanation: '', sources: [] }))
    .mockRejectedValue(new TypeError('Failed to fetch'));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await vi.waitFor(() => expect(screen.getByText('Считаем прогноз…')).toBeTruthy());
  await vi.advanceTimersByTimeAsync(5000);
  expect(screen.queryByText('Прогноз на месяц')).toBeNull();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(getForecast).toHaveBeenCalledTimes(2);
});

it('без ответа прогноза блока нет', async () => {
  vi.mocked(getPlan).mockResolvedValue(plan(null));
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Маникюр', { selector: 'h2' });
  await waitFor(() => expect(getForecast).toHaveBeenCalled());
  expect(screen.queryByText('Прогноз на месяц')).toBeNull();
});

function withAds(): Plan {
  const base = plan(null);
  return { ...base, groups: [{ ...base.groups[0], ads: [{ title: 'Маникюр у дома', text: 'Первый текст' }, { title: 'Гель-лак за час', text: 'Второй текст' }] }] };
}

it('предпросмотр показывает выбранный текст с адресом сайта и подписан как примерный', async () => {
  vi.mocked(getPlan).mockResolvedValue(withAds());
  vi.mocked(getBusiness).mockResolvedValue({ site_url: 'https://www.studio.ru/', description: null } as BusinessSettings);
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  const preview = within((await screen.findByText('Примерный вид: площадка может показать немного иначе.')).closest('figure') as HTMLElement);
  expect(preview.getByText('Маникюр у дома')).toBeTruthy();
  await waitFor(() => expect(preview.getByText('Реклама · studio.ru')).toBeTruthy());
  fireEvent.click(screen.getByRole('radio', { name: /Гель-лак за час/ }));
  expect(preview.getByText('Гель-лак за час')).toBeTruthy();
  expect(preview.getByText('Второй текст')).toBeTruthy();
});

it('если услуг в описании больше пяти, план говорит, что в Директ вошли первые пять', async () => {
  const base = plan(null);
  vi.mocked(getPlan).mockResolvedValue({ ...base, direct_services_total: 6, groups: [1, 2, 3, 4, 5].map((id) => ({ ...base.groups[0], id, name: `Услуга ${id}` })) });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  expect(await screen.findByText('В Директ вошли первые 5 услуг из описания.')).toBeTruthy();
});

it('пять услуг — строки об обрезке нет', async () => {
  const base = plan(null);
  vi.mocked(getPlan).mockResolvedValue({ ...base, direct_services_total: 5, groups: [1, 2, 3, 4, 5].map((id) => ({ ...base.groups[0], id, name: `Услуга ${id}` })) });
  render(<MemoryRouter><PlanPage /></MemoryRouter>);
  await screen.findByText('Услуга 1', { selector: 'h2' });
  expect(screen.queryByText(/В Директ вошли первые/)).toBeNull();
});
