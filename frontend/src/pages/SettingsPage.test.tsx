// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getBusiness, saveBudget, saveBusinessSettings } from '../api/settings';
import type { BusinessSettings } from '../api/settings';
import { saveMarking } from '../api/setup';
import { SettingsPage } from './SettingsPage';

vi.mock('../api/settings', async (original) => ({ ...await original<typeof import('../api/settings')>(), getBusiness: vi.fn(),
  saveBudget: vi.fn(), saveBusinessSettings: vi.fn() }));
vi.mock('../api/setup', async (original) => ({ ...await original<typeof import('../api/setup')>(), saveMarking: vi.fn() }));
vi.mock('../api/screens', () => ({ mockEnabled: false,
  getAutopilotStatus: async () => ({ status: 'running', last_check_at: null, auto_continue: true, needs_attention: [] }) }));
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
  return within(heading.closest('.card') as HTMLElement);
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
  const warning = 'Площадка ВКонтакте остановится: при таком бюджете ей не хватает минимума 300 ₽ в день.';
  vi.mocked(saveBudget).mockResolvedValue(warning);
  vi.mocked(saveBusinessSettings).mockResolvedValue();
  vi.mocked(getBusiness).mockResolvedValue(business);
  render(<MemoryRouter><SettingsPage /></MemoryRouter>);
  const block = within((await screen.findByRole('heading', { name: copy.settingsScreen.budgetTitle })).closest('.card') as HTMLElement);
  fireEvent.change(block.getByLabelText(copy.settingsScreen.budgetLabel), { target: { value: '20000' } });
  fireEvent.click(block.getByRole('button', { name: copy.settingsScreen.save }));
  expect(await block.findByText(warning)).toBeTruthy();
  expect(block.getByText(copy.settingsScreen.saved)).toBeTruthy();
  expect(saveBudget).toHaveBeenCalledWith(2000000);
});
