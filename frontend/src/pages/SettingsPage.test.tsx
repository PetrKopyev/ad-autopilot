// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getBusiness, pauseAutopilot, saveBudget, saveBusinessSettings } from '../api/settings';
import type { BusinessSettings } from '../api/settings';
import { saveDescription, saveMarking } from '../api/setup';
import { getAutopilotStatus } from '../api/screens';
import { SettingsPage } from './SettingsPage';

vi.mock('../api/settings', async (original) => ({ ...await original<typeof import('../api/settings')>(), getBusiness: vi.fn(),
  saveBudget: vi.fn(), saveBusinessSettings: vi.fn(), pauseAutopilot: vi.fn(), resumeAutopilot: vi.fn() }));
vi.mock('../api/setup', async (original) => ({ ...await original<typeof import('../api/setup')>(), saveMarking: vi.fn(), saveDescription: vi.fn(),
  getGoals: async () => ({ items: [{ id: 1, name: 'Заявка' }, { id: 2, name: 'Звонок' }] }) }));
vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })) }));
vi.mock('../api/session', () => ({ useSession: () => ({ data: { id: 1, login: 'owner', email: null, accounts: [] }, reload: () => {} }) }));

afterEach(() => { cleanup(); vi.mocked(getBusiness).mockReset(); vi.mocked(saveMarking).mockReset(); });

const business: BusinessSettings = { id: 1, site_url: 'https://example.ru', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null,
  auto_continue: true, description: null, marking: { subject_type: 'ip', inn: '', name: 'Иванов Иван', phone: '', confirmed_in_platform: { direct: true },
    saved: true, inn_masked: '•••2259', phone_masked: '+7•••1122' },
  metrica_counter_id: 12345, lead_goal_ids: [1], description_approved: true, medical_level: 'none' };

async function marking(settings = business) {
  vi.mocked(getBusiness).mockResolvedValue(settings);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const heading = await screen.findByRole('heading', { name: copy.settings.marking });
  return within(heading.closest('section') as HTMLElement);
}

it('сохранённую маркировку показывает с последними цифрами, а не пустой формой', async () => {
  const block = await marking();
  expect(block.getByText('Рекламодатель: ИП, Иванов Иван.')).toBeTruthy();
  expect(block.getByText('Данные сохранены: ИНН •••2259, телефон +7•••1122. Чтобы изменить, введите заново.')).toBeTruthy();
  expect(block.getByText('Данные рекламодателя заполнены в кабинетах: Яндекс Директ.')).toBeTruthy();
  expect(block.queryByRole('textbox')).toBeNull();
  expect(block.queryByRole('button', { name: copy.settingsScreen.save })).toBeNull();
});

it('«Изменить» открывает форму: ИНН и телефон вводят заново, «Отменить» ничего не сохраняет', async () => {
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  expect((block.getByRole('textbox', { name: copy.setup.advertiser.taxId }) as HTMLInputElement).value).toBe('');
  expect((block.getByRole('textbox', { name: copy.launchFlow.phone }) as HTMLInputElement).value).toBe('');
  expect((block.getByRole('textbox', { name: copy.launchFlow.fullName }) as HTMLInputElement).value).toBe('Иванов Иван');
  expect((block.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingCancel }));
  expect(block.getByText('Данные сохранены: ИНН •••2259, телефон +7•••1122. Чтобы изменить, введите заново.')).toBeTruthy();
  expect(saveMarking).not.toHaveBeenCalled();
});

it('после сохранения отправляет все поля и показывает новые последние цифры', async () => {
  vi.mocked(saveMarking).mockResolvedValue();
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  fireEvent.change(block.getByRole('textbox', { name: copy.setup.advertiser.taxId }), { target: { value: '500100732259' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.phone }), { target: { value: '+7 900 000-33-44' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText('Данные сохранены: ИНН •••2259, телефон +7•••3344. Чтобы изменить, введите заново.')).toBeTruthy();
  expect(block.getByRole('status').textContent).toBe(copy.settingsScreen.markingSaved);
  expect(saveMarking).toHaveBeenCalledWith({ subject_type: 'ip', inn: '500100732259', name: 'Иванов Иван', phone: '+7 900 000-33-44',
    confirmed_in_platform: { direct: true, vk: false } });
});

it('без сохранённой маркировки сразу показывает форму', async () => {
  const block = await marking({ ...business, marking: null });
  expect((block.getByRole('textbox', { name: copy.setup.advertiser.taxId }) as HTMLInputElement).value).toBe('');
  expect(block.queryByRole('button', { name: copy.settingsScreen.markingCancel })).toBeNull();
});

it('бюджет ниже порога ВКонтакте сохраняется, а предупреждение сервера видно под формой', async () => {
  const warning = 'Площадка ВКонтакте остановлена: при таком бюджете ей не хватает минимума 300 ₽ в день.';
  vi.mocked(saveBudget).mockResolvedValue(warning);
  vi.mocked(saveBusinessSettings).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settingsScreen.budgetTitle })).closest('section') as HTMLElement);
  fireEvent.change(block.getByLabelText(copy.settingsScreen.budgetLabel), { target: { value: '20000' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(warning)).toBeTruthy();
  expect(block.getByText(copy.settingsScreen.saved)).toBeTruthy();
  expect(saveBudget).toHaveBeenCalledWith(2000000);
});

it('средний чек — только цифры, как бюджет; неизменённый уходит прежней суммой', async () => {
  vi.mocked(saveBusinessSettings).mockReset().mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue({ ...business, avg_check_kop: 150050 });
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settingsScreen.budgetTitle })).closest('section') as HTMLElement);
  const average = block.getByLabelText(copy.settingsScreen.averageLabel) as HTMLInputElement;
  expect(average.value).toBe('1 501');
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  await block.findByText(copy.settingsScreen.saved);
  expect(saveBusinessSettings).toHaveBeenLastCalledWith(true, 150050);
  fireEvent.change(average, { target: { value: '2 000 руб' } });
  expect(average.value).toBe('2 000');
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  await vi.waitFor(() => expect(saveBusinessSettings).toHaveBeenLastCalledWith(true, 200000));
});

it('услуги и цены в описании — одно поле, отдельное поле цен не показывается и уходит пустым', async () => {
  vi.mocked(saveDescription).mockResolvedValue();
  const field = (value: string) => ({ value, quote: null, verified: true, question: '' });
  vi.mocked(getBusiness).mockResolvedValue({ ...business, description: { fields: {
    services: field('Маникюр — 1 500 ₽\nПедикюр'), prices: field('Маникюр — 1 500 ₽'), address: field(''), difference: field('') } } });
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.business })).closest('section') as HTMLElement);
  expect((block.getByLabelText(copy.settingsScreen.description.services) as HTMLTextAreaElement).value).toBe('Маникюр — 1 500 ₽\nПедикюр');
  expect(block.queryByLabelText(copy.settingsScreen.description.prices)).toBeNull();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  await vi.waitFor(() => expect(saveDescription).toHaveBeenCalled());
  const [sent] = vi.mocked(saveDescription).mock.calls[0];
  expect([sent.services.value, sent.prices.value]).toEqual(['Маникюр — 1 500 ₽\nПедикюр', '']);
});

it('режим автопилота — «Работает / Пауза»: «Пауза» останавливает автопилот, повторный выбор текущего режима ничего не шлёт', async () => {
  vi.mocked(pauseAutopilot).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const mode = within(await screen.findByRole('group', { name: copy.autopilotMode.label }));
  expect(mode.getByRole('button', { name: copy.autopilotMode.working }).getAttribute('aria-pressed')).toBe('true');
  fireEvent.click(mode.getByRole('button', { name: copy.autopilotMode.working }));
  expect(pauseAutopilot).not.toHaveBeenCalled();
  fireEvent.click(mode.getByRole('button', { name: copy.autopilotMode.paused }));
  await vi.waitFor(() => expect(pauseAutopilot).toHaveBeenCalledTimes(1));
});

it('кабинеты показывают состояние словами, а не только цветом', async () => {
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  expect(block.getAllByText(copy.setup.accounts.disconnected)).toHaveLength(2);
  expect(block.getAllByRole('button', { name: copy.settings.connect })).toHaveLength(2);
});

it('Метрика в кабинетах: счётчик и цели словами; после запуска их не меняют', async () => {
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  expect(await block.findByText(copy.settingsScreen.metricaLine('12345', '«Заявка»'))).toBeTruthy();
  expect(block.getByText(copy.settingsScreen.metricaLocked)).toBeTruthy();
  expect(block.queryByRole('link', { name: copy.settingsScreen.metricaEdit })).toBeNull();
});

it('до запуска счётчик и цели меняются в мастере: «Изменить» ведёт на его шаг', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'draft', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true });
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  expect((await block.findByRole('link', { name: copy.settingsScreen.metricaEdit })).getAttribute('href')).toBe('/setup/2');
  expect(block.queryByText(copy.settingsScreen.metricaLocked)).toBeNull();
});
