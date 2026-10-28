// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { SessionProvider, useSession } from '../api/session';
import { SetupPage } from './SetupPage';

const fetchMock = vi.fn<typeof fetch>();
let hasBusiness = false;
beforeEach(() => {
  hasBusiness = false;
  vi.spyOn(document, 'cookie', 'get').mockReturnValue('__Host-csrf=token');
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (init?.method === 'POST' && path === '/api/business') hasBusiness = true;
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: hasBusiness, accounts: [] },
      '/api/business': { id: 7, site_url: 'https://salon.example/', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null },
    };
    return new Response(JSON.stringify(answers[path]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); fetchMock.mockReset(); sessionStorage.clear(); });

function Probe() {
  return <><p data-testid="path">{useLocation().pathname}</p><p data-testid="business">{String(useSession().data?.has_business)}</p></>;
}
const calls = () => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${new URL(String(url)).pathname}`);

it('после первого шага сессия перечитывается: признак бизнеса пускает в разделы с данными, а не возвращает в мастер', async () => {
  render(<MemoryRouter initialEntries={['/setup/1']}><SessionProvider><Routes>
    <Route path="setup/:step" element={<SetupPage />} />
  </Routes><Probe /></SessionProvider></MemoryRouter>);
  await waitFor(() => expect(screen.getByTestId('business').textContent).toBe('false'));
  fireEvent.change(screen.getByLabelText(copy.setup.business.website), { target: { value: 'https://salon.example/' } });
  fireEvent.change(screen.getByLabelText(copy.setup.business.city), { target: { value: 'Москва' } });
  fireEvent.change(screen.getByLabelText(copy.setup.business.budget), { target: { value: '30000' } });
  fireEvent.click(screen.getByRole('button', { name: copy.common.next }));
  await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/setup/2'));
  await waitFor(() => expect(screen.getByTestId('business').textContent).toBe('true'));
  expect(calls().slice(-2)).toEqual(['POST /api/business', 'GET /api/me']);
});

const field = (value: string) => ({ value, quote: value || null, verified: !!value, question: '' });
const description = { services: field('Маникюр'), prices: field(''), address: field(''), difference: field('') };
const busy = 'Изменение этого бизнеса уже выполняется. Попробуйте позже.';
// Мастер дошёл до последнего шага; сохранение отвечает 409 с текстом сервера, остальное — как обычно.
function conflictOn(put: string, detail: string) {
  sessionStorage.setItem('ad-autopilot-setup-draft', JSON.stringify({
    business: { id: 7, site_url: 'https://salon.example/', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null },
    counter: 12345, goals: [1], description, approved: true, maxStep: 5,
    site: { status: 'ready', pages_read: 2, services_found: 1, counter_id: 12345, description, medical_level: 'none' },
  }));
  fetchMock.mockImplementation(async (url, init) => {
    const path = new URL(String(url)).pathname;
    if (init?.method === 'PUT' && path === put) {
      return new Response(JSON.stringify({ detail }), { status: 409, headers: { 'Content-Type': 'application/json' } });
    }
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: true,
        accounts: [{ id: 1, business_id: 7, platform: 'direct', external_login: 'owner', status: 'ok', scopes: [] }] },
      '/api/metrica/counters': { items: [{ id: 12345, name: 'Счётчик' }] },
      '/api/metrica/goals': { items: [{ id: 1, name: 'Заявка' }] },
    };
    return new Response(JSON.stringify(answers[path] ?? {}), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
}
function renderStep(step: number) {
  render(<MemoryRouter initialEntries={[`/setup/${step}`]}><SessionProvider><Routes>
    <Route path="setup/:step" element={<SetupPage />} />
  </Routes><Probe /></SessionProvider></MemoryRouter>);
}

it('409 на шаге Метрики показывает текст сервера и оставляет выбранные счётчик и цели', async () => {
  const launched = 'Реклама уже запущена. Счётчик и цели Метрики после запуска не меняются.';
  conflictOn('/api/business/metrica', launched);
  renderStep(2);
  const goal = await screen.findByRole('checkbox', { name: 'Заявка' });
  await waitFor(() => expect((screen.getByLabelText(copy.setup.accounts.counter) as HTMLSelectElement).value).toBe('12345'));
  fireEvent.click(screen.getByRole('button', { name: copy.common.next }));
  expect((await screen.findByRole('alert')).textContent).toContain(launched);
  expect(screen.getByTestId('path').textContent).toBe('/setup/2');
  expect((screen.getByLabelText(copy.setup.accounts.counter) as HTMLSelectElement).value).toBe('12345');
  expect((goal as HTMLInputElement).checked).toBe(true);
  expect(calls()).toContain('PUT /api/business/metrica');
});

it('409 на шаге описания показывает текст сервера и не теряет правку и подтверждение', async () => {
  conflictOn('/api/business/description', busy);
  renderStep(4);
  const services = await screen.findByLabelText(copy.setup.draft.services);
  fireEvent.change(services, { target: { value: 'Маникюр и педикюр' } });
  fireEvent.click(screen.getByRole('checkbox', { name: copy.launchFlow.approved }));
  fireEvent.click(screen.getByRole('button', { name: copy.common.next }));
  expect((await screen.findByRole('alert')).textContent).toContain(busy);
  expect(screen.getByTestId('path').textContent).toBe('/setup/4');
  expect((screen.getByLabelText(copy.setup.draft.services) as HTMLTextAreaElement).value).toBe('Маникюр и педикюр');
  expect((screen.getByRole('checkbox', { name: copy.launchFlow.approved }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByRole('button', { name: copy.common.next }).hasAttribute('disabled')).toBe(false);
});

it('409 на шаге маркировки показывает текст сервера и оставляет данные рекламодателя', async () => {
  conflictOn('/api/business/marking', busy);
  renderStep(5);
  fireEvent.change(await screen.findByLabelText(copy.setup.advertiser.taxId), { target: { value: '526317984689' } });
  fireEvent.change(screen.getByLabelText(copy.launchFlow.fullName), { target: { value: 'ИП Иванов' } });
  fireEvent.change(screen.getByLabelText(copy.launchFlow.phone), { target: { value: '+7 900 123-45-67' } });
  fireEvent.click(screen.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }));
  fireEvent.click(screen.getByRole('button', { name: copy.plan.title }));
  expect((await screen.findByRole('alert')).textContent).toContain(busy);
  expect(screen.getByTestId('path').textContent).toBe('/setup/5');
  expect((screen.getByLabelText(copy.setup.advertiser.taxId) as HTMLInputElement).value).toBe('526317984689');
  expect((screen.getByLabelText(copy.launchFlow.fullName) as HTMLInputElement).value).toBe('ИП Иванов');
  expect((screen.getByLabelText(copy.launchFlow.phone) as HTMLInputElement).value).toBe('+7 900 123-45-67');
  expect((screen.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }) as HTMLInputElement).checked).toBe(true);
});
