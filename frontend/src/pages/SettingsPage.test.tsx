// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getBusiness, pauseAutopilot, resumeAutopilot, saveBudget, saveBusinessSettings, saveVkGoal } from '../api/settings';
import type { BusinessSettings } from '../api/settings';
import { connectAccount, disconnectAccount, getAuthConfig, saveDescription, saveMarking } from '../api/setup';
import type { Account } from '../api/setup';
import { getAutopilotStatus } from '../api/screens';
import { SettingsPage } from './SettingsPage';

vi.mock('../api/settings', async (original) => ({ ...await original<typeof import('../api/settings')>(), getBusiness: vi.fn(),
  saveBudget: vi.fn(), saveBusinessSettings: vi.fn(), saveVkGoal: vi.fn(), pauseAutopilot: vi.fn(), resumeAutopilot: vi.fn() }));
vi.mock('../api/setup', async (original) => ({ ...await original<typeof import('../api/setup')>(), saveMarking: vi.fn(), saveDescription: vi.fn(),
  connectAccount: vi.fn(), disconnectAccount: vi.fn(), getAuthConfig: vi.fn(async () => ({ demo: false, vk_oauth: false })),
  getGoals: async () => ({ items: [{ id: 1, name: 'Заявка' }, { id: 2, name: 'Звонок' }] }) }));
vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: vi.fn(async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true })) }));
let mockAccounts: Account[] = [];
vi.mock('../api/session', () => ({ useSession: () => ({ data: { id: 1, login: 'owner', email: null, accounts: mockAccounts }, reload: () => {} }) }));

afterEach(() => { cleanup(); mockAccounts = []; vi.mocked(getBusiness).mockReset(); vi.mocked(saveMarking).mockReset(); vi.mocked(disconnectAccount).mockReset(); vi.mocked(connectAccount).mockReset(); vi.mocked(saveVkGoal).mockReset(); });

const business: BusinessSettings = { id: 1, site_url: 'https://example.ru', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null,
  auto_continue: true, description: null, marking: { subject_type: 'ip', inn: '', name: 'Иванов Иван', phone: '', confirmed_in_platform: { direct: true },
    saved: true, inn_masked: '•••2259', phone_masked: '+7•••1122', transmission: { direct: 'marking_transmitted', vk: null } },
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
  vi.mocked(saveMarking).mockResolvedValue({ direct: 'marking_transmitted', vk: null });
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  fireEvent.change(block.getByRole('textbox', { name: copy.setup.advertiser.taxId }), { target: { value: '500100732259' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.phone }), { target: { value: '+7 900 000-33-44' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText('Данные сохранены: ИНН •••2259, телефон +7•••3344. Чтобы изменить, введите заново.')).toBeTruthy();
  expect(block.getByRole('status').textContent).toBe(copy.settingsScreen.markingSaved);
  expect(saveMarking).toHaveBeenCalledWith({ subject_type: 'ip', inn: '500100732259', name: 'Иванов Иван', phone: '+7 (900) 000-33-44',
    confirmed_in_platform: { direct: true, vk: false } });
});

it('неполный телефон: ошибка у поля, как в мастере, а не общая', async () => {
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  fireEvent.change(block.getByRole('textbox', { name: copy.setup.advertiser.taxId }), { target: { value: '500100732259' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.phone }), { target: { value: '+7 (999) 123-45-6' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  const phone = block.getByRole('textbox', { name: copy.launchFlow.phone });
  expect(phone.getAttribute('aria-invalid')).toBe('true');
  expect(document.getElementById('settings-phone-error')?.textContent).toBe(copy.setup.advertiser.phoneError);
  expect(block.getAllByText(copy.setup.advertiser.phoneError)).toHaveLength(1);
  expect(block.queryByText(copy.launchFlow.markingError)).toBeNull();
  expect(saveMarking).not.toHaveBeenCalled();
});

it('пустая форма: без подсказок браузера, ошибки по-русски у полей и по одной', async () => {
  const block = await marking({ ...business, marking: null });
  const form = block.getByRole('button', { name: copy.settingsScreen.save }).closest('form') as HTMLFormElement;
  expect(form.noValidate).toBe(true);
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(block.getAllByText(copy.launchFlow.innError('ip'))).toHaveLength(1);
  expect(block.getAllByText(copy.setup.advertiser.phoneError)).toHaveLength(1);
  expect(block.getAllByText(copy.setup.advertiser.nameError)).toHaveLength(1);
  expect(saveMarking).not.toHaveBeenCalled();
});

it('без ФИО — ошибка у поля, а не общая', async () => {
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  fireEvent.change(block.getByRole('textbox', { name: copy.setup.advertiser.taxId }), { target: { value: '500100732259' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.phone }), { target: { value: '+7 900 000-33-44' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.fullName }), { target: { value: ' ' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  const name = block.getByRole('textbox', { name: copy.launchFlow.fullName });
  expect(name.getAttribute('aria-invalid')).toBe('true');
  expect(document.getElementById('settings-name-error')?.textContent).toBe(copy.setup.advertiser.nameError);
  expect(block.getAllByText(copy.setup.advertiser.nameError)).toHaveLength(1);
  expect(block.queryByText(copy.launchFlow.markingError)).toBeNull();
  expect(saveMarking).not.toHaveBeenCalled();
});

it('подтверждение маркировки во ВКонтакте — только когда ВКонтакте подключён', async () => {
  mockAccounts = [direct];
  let block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  expect(block.getByRole('checkbox', { name: copy.launchFlow.confirmDirect })).toBeTruthy();
  expect(block.queryByRole('checkbox', { name: copy.launchFlow.confirmVk })).toBeNull();
  cleanup();
  mockAccounts = [direct, { ...direct, id: 6, platform: 'vk' }];
  block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  expect(block.getByRole('checkbox', { name: copy.launchFlow.confirmVk })).toBeTruthy();
  // Отключённый или требующий входа кабинет VK — не подключён: подтверждать в нём нечего.
  for (const status of ['revoked', 'needs_reauth'] as const) {
    cleanup();
    mockAccounts = [direct, { ...direct, id: 6, platform: 'vk', status }];
    block = await marking();
    fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
    expect(block.queryByRole('checkbox', { name: copy.launchFlow.confirmVk })).toBeNull();
  }
});

it('без сохранённой маркировки сразу показывает форму', async () => {
  const block = await marking({ ...business, marking: null });
  expect((block.getByRole('textbox', { name: copy.setup.advertiser.taxId }) as HTMLInputElement).value).toBe('');
  expect(block.queryByRole('button', { name: copy.settingsScreen.markingCancel })).toBeNull();
});

it('переданные данные маркировки показаны словами; отказ площадки — с инструкцией по кабинету', async () => {
  const block = await marking({ ...business, marking: { ...business.marking!,
    confirmed_in_platform: { direct: false }, transmission: { direct: 'marking_manual_required', vk: null } } });
  expect(block.getByText(copy.marking.manualRequired(copy.setup.accounts.direct))).toBeTruthy();
  for (const step of copy.marking.manualSteps.direct) expect(block.getByText(step)).toBeTruthy();
  expect(block.getByText(copy.marking.confirmAfterSteps)).toBeTruthy();
  expect(block.queryByText(copy.marking.transmitted(copy.setup.accounts.direct))).toBeNull();
});

it('площадка, принявшая данные маркировки, названа в статусе передачи', async () => {
  const block = await marking();
  expect(block.getByText(copy.marking.transmitted(copy.setup.accounts.direct))).toBeTruthy();
  expect(block.queryByText(copy.marking.manualRequired(copy.setup.accounts.direct))).toBeNull();
});

it('после сохранения маркировки статус передачи из ответа сервера виден сразу', async () => {
  vi.mocked(saveMarking).mockResolvedValue({ direct: 'marking_manual_required', vk: null });
  const block = await marking();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.markingEdit }));
  fireEvent.click(block.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }));
  fireEvent.change(block.getByRole('textbox', { name: copy.setup.advertiser.taxId }), { target: { value: '500100732259' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.launchFlow.phone }), { target: { value: '+7 900 000-33-44' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(copy.marking.manualRequired(copy.setup.accounts.direct))).toBeTruthy();
});

it('бюджет: отклонённое сохранение возвращает поле к действующей сумме', async () => {
  vi.mocked(saveBudget).mockReset().mockRejectedValue(new Error('Минимальный бюджет — 15 500 ₽ в месяц.'));
  vi.mocked(saveBusinessSettings).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settingsScreen.budgetTitle })).closest('section') as HTMLElement);
  const field = block.getByLabelText(copy.settingsScreen.budgetLabel) as HTMLInputElement;
  fireEvent.change(field, { target: { value: '10000' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText('Минимальный бюджет — 15 500 ₽ в месяц.')).toBeTruthy();
  // Отказ сервера: в поле осталась реально сохранённая сумма, а не введённая, и подсказка о будущем бюджете исчезла.
  expect(field.value).toBe('30\u00a0000');
  expect(block.queryByText(/После сохранения бюджет станет/)).toBeNull();
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
  // Поле названо так же, как на шаге 1 мастера: владелец узнаёт ту же сумму.
  expect(copy.settingsScreen.averageLabel.startsWith(copy.setup.business.visitPrice)).toBe(true);
  expect(average.value).toBe('1 500,50');
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

it('остановка в настройках — та же остановка всей рекламы, что на главной: одна кнопка, сначала подтверждение', async () => {
  vi.mocked(pauseAutopilot).mockReset().mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const stop = await screen.findByRole('button', { name: copy.home.stopAll });
  expect(screen.getByText(copy.settingsScreen.autopilotRunning)).toBeTruthy();
  expect(screen.queryByRole('group', { name: copy.autopilotMode.label })).toBeNull();
  fireEvent.click(stop);
  const dialog = within(screen.getByRole('dialog', { name: copy.screens.stopTitle }));
  expect(dialog.getByText(copy.screens.stopDescription)).toBeTruthy();
  fireEvent.click(dialog.getByRole('button', { name: copy.screens.cancel }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(pauseAutopilot).not.toHaveBeenCalled();
  fireEvent.click(stop);
  fireEvent.click(screen.getByRole('button', { name: copy.screens.confirmStop }));
  await vi.waitFor(() => expect(pauseAutopilot).toHaveBeenCalledTimes(1));
  await vi.waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('«Продолжить» включил не все кампании: в настройках кнопка «Продолжить» есть и у работающей рекламы', async () => {
  vi.mocked(getAutopilotStatus).mockResolvedValueOnce({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [], has_plan: true, resume_pending: true });
  vi.mocked(resumeAutopilot).mockReset().mockResolvedValue(null);
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  expect(await screen.findByText(copy.home.resumeLeft)).toBeTruthy();
  expect(screen.getByRole('button', { name: copy.home.stopAll })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: copy.home.resumeAll }));
  await vi.waitFor(() => expect(resumeAutopilot).toHaveBeenCalledTimes(1));
});

it('бюджет: копейки через точку, сумма больше предела — ошибка у поля; подпись до сохранения говорит «после сохранения»', async () => {
  vi.mocked(saveBudget).mockReset().mockResolvedValue(null);
  vi.mocked(saveBusinessSettings).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settingsScreen.budgetTitle })).closest('section') as HTMLElement);
  const field = block.getByLabelText(copy.settingsScreen.budgetLabel);
  fireEvent.change(field, { target: { value: '50000000' } });
  expect(block.getByText(copy.launchFlow.moneyTooBig)).toBeTruthy();
  expect((block.getByRole('button', { name: copy.settingsScreen.save }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(field, { target: { value: '35000.5' } });
  expect(block.getByText(copy.settingsScreen.budgetPending(`${(35000.5).toLocaleString('ru-RU', { minimumFractionDigits: 2 })}\u00a0₽`), { normalizer: (text) => text })).toBeTruthy();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  await vi.waitFor(() => expect(saveBudget).toHaveBeenCalledWith(3500050));
});

it('кабинеты показывают состояние словами, а не только цветом; ВКонтакте подключают на своей странице', async () => {
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  expect(block.getAllByText(copy.setup.accounts.disconnected)).toHaveLength(2);
  expect(block.getByRole('button', { name: copy.settings.connect })).toBeTruthy();
  expect(block.getByRole('link', { name: copy.settings.connect }).getAttribute('href')).toBe('/connect/vk');
});

const direct: Account = { id: 5, business_id: 1, platform: 'direct', external_login: 'salon', status: 'ok', scopes: [] };

it('«Отключить» кабинет — только после подтверждения, где сказано, что будет с рекламой', async () => {
  mockAccounts = [direct];
  vi.mocked(disconnectAccount).mockResolvedValue({ stopped: true, message: 'Кабинет отключён. Рекламу на этой площадке перед отключением остановили.' });
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  fireEvent.click(block.getByRole('button', { name: copy.settings.disconnect }));
  const dialog = within(screen.getByRole('dialog', { name: copy.settings.disconnectTitle(copy.setup.accounts.direct) }));
  expect(dialog.getByText(copy.settings.disconnectSteps[0])).toBeTruthy();
  expect(dialog.queryByText(copy.settings.disconnectDemo)).toBeNull();
  expect(disconnectAccount).not.toHaveBeenCalled();
  fireEvent.click(dialog.getByRole('button', { name: copy.settings.disconnectYes }));
  await vi.waitFor(() => expect(disconnectAccount).toHaveBeenCalledWith(5));
  expect(await screen.findByText('Кабинет отключён. Рекламу на этой площадке перед отключением остановили.')).toBeTruthy();
  expect(screen.queryByRole('dialog')).toBeNull();
});

it('в демо подтверждение отключения предупреждает, что подключить заново нельзя; ВКонтакте — кнопкой с понятной ошибкой', async () => {
  mockAccounts = [direct];
  // Демо спрашивают и описание, и кабинеты.
  vi.mocked(getAuthConfig).mockResolvedValueOnce({ demo: true, vk_oauth: false }).mockResolvedValueOnce({ demo: true, vk_oauth: false });
  vi.mocked(connectAccount).mockRejectedValue(new Error(copy.errors.demoConnect));
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  const vk = await block.findByRole('button', { name: copy.settings.connect });
  fireEvent.click(block.getByRole('button', { name: copy.settings.disconnect }));
  expect(within(screen.getByRole('dialog')).getByText(copy.settings.disconnectDemo)).toBeTruthy();
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: copy.screens.cancel }));
  fireEvent.click(vk);
  expect(await block.findByText(copy.errors.demoConnect)).toBeTruthy();
});

it('в демо отключённый Директ подключается заново кнопкой «Подключить»', async () => {
  mockAccounts = [{ ...direct, status: 'revoked' }];
  // Демо спрашивают и описание, и кабинеты.
  vi.mocked(getAuthConfig).mockResolvedValueOnce({ demo: true, vk_oauth: false }).mockResolvedValueOnce({ demo: true, vk_oauth: false });
  vi.mocked(connectAccount).mockResolvedValue(undefined);
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.accounts })).closest('section') as HTMLElement);
  await vi.waitFor(() => expect(block.getAllByRole('button', { name: copy.settings.connect })).toHaveLength(2));
  fireEvent.click(block.getAllByRole('button', { name: copy.settings.connect })[0]);
  await vi.waitFor(() => expect(connectAccount).toHaveBeenCalledWith('direct', 1));
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

it('после запуска описание честно говорит, что запущенные объявления не перестроятся', async () => {
  vi.mocked(saveDescription).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.business })).closest('section') as HTMLElement);
  expect(await block.findByText(new RegExp(copy.settingsScreen.businessLaunched))).toBeTruthy();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(copy.settingsScreen.savedLaunched)).toBeTruthy();
});

it('в демо после правки описания подсказывает перемотать время: без неё убранная услуга не остановится', async () => {
  vi.mocked(getAuthConfig).mockResolvedValueOnce({ demo: true, vk_oauth: false }).mockResolvedValueOnce({ demo: true, vk_oauth: false });
  vi.mocked(saveDescription).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settings.business })).closest('section') as HTMLElement);
  await act(async () => {});
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(copy.settingsScreen.savedLaunchedDemo)).toBeTruthy();
});

async function vkGoal(settings: BusinessSettings = business) {
  vi.mocked(getBusiness).mockResolvedValue(settings);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const heading = await screen.findByRole('heading', { name: copy.settingsScreen.vkGoal });
  return within(heading.closest('form') as HTMLElement);
}

it('цель ВКонтакте видна только при подключённом кабинете VK', async () => {
  mockAccounts = [direct];
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  await screen.findByRole('heading', { name: copy.settings.accounts });
  expect(screen.queryByRole('heading', { name: copy.settingsScreen.vkGoal })).toBeNull();
});

it('цель ВКонтакте: отправляет номер пикселя и название и показывает сохранённое', async () => {
  mockAccounts = [direct, { ...direct, id: 6, platform: 'vk' }];
  vi.mocked(saveVkGoal).mockResolvedValue({ ...business, vk_priced_goal_source_id: 4242, vk_priced_goal_name: 'Запись онлайн' });
  const block = await vkGoal();
  expect(block.getByText(copy.settingsScreen.vkGoalNone)).toBeTruthy();
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(block.getByText(copy.settingsScreen.vkGoalPixelError)).toBeTruthy();
  expect(saveVkGoal).not.toHaveBeenCalled();
  fireEvent.change(block.getByRole('textbox', { name: copy.settingsScreen.vkGoalPixel }), { target: { value: '4242' } });
  fireEvent.change(block.getByRole('textbox', { name: copy.settingsScreen.vkGoalName }), { target: { value: ' Запись онлайн ' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(copy.settingsScreen.vkGoalLine(4242, 'Запись онлайн'))).toBeTruthy();
  expect(saveVkGoal).toHaveBeenCalledWith(4242, 'Запись онлайн');
  expect(block.getByRole('status').textContent).toBe(copy.settingsScreen.vkGoalSaved);
});

it('цель ВКонтакте: сохранённая видна сразу, «Убрать цель» отправляет 0', async () => {
  mockAccounts = [direct, { ...direct, id: 6, platform: 'vk' }];
  vi.mocked(saveVkGoal).mockResolvedValue({ ...business, vk_priced_goal_source_id: null, vk_priced_goal_name: null });
  const block = await vkGoal({ ...business, vk_priced_goal_source_id: 77, vk_priced_goal_name: 'Заявка' });
  expect(block.getByText(copy.settingsScreen.vkGoalLine(77, 'Заявка'))).toBeTruthy();
  expect((block.getByRole('textbox', { name: copy.settingsScreen.vkGoalPixel }) as HTMLInputElement).value).toBe('77');
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.vkGoalRemove }));
  expect(await block.findByText(copy.settingsScreen.vkGoalNone)).toBeTruthy();
  expect(saveVkGoal).toHaveBeenCalledWith(0, '');
});
