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
      '/api/cities': { cities: ['Казань', 'Москва', 'Москва и Московская область'] },
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

it('город сохраняется только из списка: свободный текст не отправляется, выбранный — уходит названием из списка', async () => {
  render(<MemoryRouter initialEntries={['/setup/1']}><SessionProvider><Routes>
    <Route path="setup/:step" element={<SetupPage />} />
  </Routes><Probe /></SessionProvider></MemoryRouter>);
  await waitFor(() => expect(calls()).toContain('GET /api/cities'));
  fireEvent.change(screen.getByLabelText(copy.setup.business.website), { target: { value: 'https://salon.example/' } });
  fireEvent.change(screen.getByLabelText(copy.setup.business.budget), { target: { value: '30 000 руб' } });
  const city = screen.getByLabelText(copy.setup.business.city);
  fireEvent.change(city, { target: { value: 'Мск' } });
  fireEvent.click(screen.getByRole('button', { name: copy.common.next }));
  expect(await screen.findByText(copy.setup.business.cityError)).toBeTruthy();
  expect(calls()).not.toContain('POST /api/business');
  fireEvent.change(city, { target: { value: 'моск' } });
  expect(screen.getAllByRole('option').map((item) => item.textContent)).toEqual(['Москва', 'Москва и Московская область']);
  fireEvent.click(screen.getByRole('option', { name: 'Москва' }));
  fireEvent.click(screen.getByRole('button', { name: copy.common.next }));
  await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/setup/2'));
  const [, init] = fetchMock.mock.calls.find(([url, request]) => request?.method === 'POST' && String(url).endsWith('/api/business'))!;
  expect(JSON.parse(String(init!.body))).toMatchObject({ city: 'Москва', month_budget_kop: 3_000_000 });
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

it('вход на шаг сайта берёт проверку из кэша, а «Попробовать ещё раз» просит перечитать сайт', async () => {
  conflictOn('/api/business/description', busy);
  const answer = fetchMock.getMockImplementation()!;
  const site = (found: typeof description) => ({ status: 'ready', pages_read: 2, services_found: 1, counter_id: 12345, description: found, medical_level: 'none' });
  const found = { ...description, services: field('Педикюр'), prices: field('Маникюр — 1 500 ₽') };
  fetchMock.mockImplementation(async (url, init) => {
    const check = new URL(String(url));
    return check.pathname === '/api/business/site-check'
      ? new Response(JSON.stringify(site(check.search ? found : description)), { status: 200, headers: { 'Content-Type': 'application/json' } })
      : answer(url, init);
  });
  const checks = () => fetchMock.mock.calls.map(([url]) => new URL(String(url))).filter((url) => url.pathname === '/api/business/site-check').map((url) => url.search);
  renderStep(3);
  const retry = await screen.findByRole('button', { name: copy.common.retry });
  await waitFor(() => expect(retry.hasAttribute('disabled')).toBe(false));
  expect(checks()).toEqual(['']);
  fireEvent.click(retry);
  await waitFor(() => expect(checks()).toEqual(['', '?refresh=1']));
  await waitFor(() => expect(retry.hasAttribute('disabled')).toBe(false));
  expect(checks()).toEqual(['', '?refresh=1']);
  // Повтор заполнил пустую цену и не тронул услуги, которые уже были в форме.
  const stored = JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!).description;
  expect([stored.services.value, stored.prices.value]).toEqual(['Маникюр', 'Маникюр — 1 500 ₽']);
});

it('обычный вход на шаг сайта дополняет пустые поля черновика и не затирает введённое', async () => {
  conflictOn('/api/business/description', busy);
  const answer = fetchMock.getMockImplementation()!;
  const found = { ...description, services: field('Педикюр'), address: field('Самара, ул. Полевая, д. 7') };
  fetchMock.mockImplementation(async (url, init) => new URL(String(url)).pathname === '/api/business/site-check'
    ? new Response(JSON.stringify({ status: 'ready', pages_read: 2, services_found: 1, counter_id: 12345, description: found, medical_level: 'none' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } })
    : answer(url, init));
  renderStep(3);
  await waitFor(() => expect(JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!).description.address.value).toBe('Самара, ул. Полевая, д. 7'));
  expect(JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!).description.services.value).toBe('Маникюр');
});

// В вкладке черновика нет, а на сервере мастер пройден до конца.
function savedOnServer() {
  fetchMock.mockImplementation(async (url) => {
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: true,
        accounts: [{ id: 1, business_id: 7, platform: 'direct', external_login: 'owner', status: 'ok', scopes: [] }] },
      '/api/business': { id: 7, site_url: 'https://salon.example/', city: 'Самара', month_budget_kop: 3000000, avg_check_kop: 150000,
        description: { fields: description }, auto_continue: true, metrica_counter_id: 12345, lead_goal_ids: [1], description_approved: true,
        medical_level: 'none', marking: { subject_type: 'ooo', inn: '', name: 'ООО Ромашка', phone: '', confirmed_in_platform: { direct: true, vk: false },
          saved: true, inn_masked: '•••3333', phone_masked: '+7•••1122' } },
      '/api/metrica/counters': { items: [{ id: 12345, name: 'Счётчик' }] },
      '/api/metrica/goals': { items: [{ id: 1, name: 'Заявка' }] },
    };
    return new Response(JSON.stringify(answers[new URL(String(url)).pathname] ?? {}), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
}

it('без черновика в вкладке шаги восстанавливаются с сервера: прямой заход на шаг описания не уводит на первый', async () => {
  savedOnServer();
  renderStep(4);
  expect(screen.getByRole('status').className).toBe('loading');
  expect(((await screen.findByLabelText(copy.setup.draft.services)) as HTMLTextAreaElement).value).toBe('Маникюр');
  expect(screen.getByTestId('path').textContent).toBe('/setup/4');
  expect((screen.getByRole('checkbox', { name: copy.launchFlow.approved }) as HTMLInputElement).checked).toBe(true);
  expect(calls()).not.toContain('GET /api/business/site-check');
  expect(JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!)).toMatchObject({ user: 1, counter: 12345, goals: [1], maxStep: 5 });
  fireEvent.click(screen.getByRole('link', { name: copy.common.back }));
  fireEvent.click(await screen.findByRole('link', { name: copy.common.back }));
  await waitFor(() => expect((screen.getByLabelText(copy.setup.accounts.counter) as HTMLSelectElement).value).toBe('12345'));
  expect((await screen.findByRole('checkbox', { name: 'Заявка' }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole('link', { name: copy.common.back }));
  expect((await screen.findByLabelText(copy.setup.business.city) as HTMLInputElement).value).toBe('Самара');
  expect((screen.getByLabelText(copy.setup.business.budget) as HTMLInputElement).value).toBe('30\u00a0000');
  expect((screen.getByLabelText(copy.setup.business.visitPrice) as HTMLInputElement).value).toBe('1500');
});

it('шаг маркировки восстанавливает реквизиты и без правок ведёт к плану, не отправляя их заново', async () => {
  savedOnServer();
  renderStep(5);
  const name = await screen.findByLabelText(copy.launchFlow.fullName) as HTMLInputElement;
  await waitFor(() => expect(name.value).toBe('ООО Ромашка'));
  expect((screen.getByLabelText(copy.setup.advertiser.type) as HTMLSelectElement).value).toBe('ooo');
  expect((screen.getByLabelText(copy.setup.advertiser.taxId) as HTMLInputElement).value).toBe('');
  expect((screen.getByRole('checkbox', { name: copy.launchFlow.confirmDirect }) as HTMLInputElement).checked).toBe(true);
  expect(screen.getByText(copy.settingsScreen.markingStored('•••3333', '+7•••1122'))).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: copy.plan.title }));
  await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/plan'));
  expect(calls()).not.toContain('PUT /api/business/marking');
});

it('изменённые реквизиты без ИНН не сохраняются: их нужно ввести заново', async () => {
  savedOnServer();
  renderStep(5);
  const name = await screen.findByLabelText(copy.launchFlow.fullName) as HTMLInputElement;
  await waitFor(() => expect(name.value).toBe('ООО Ромашка'));
  fireEvent.change(name, { target: { value: 'ООО Лютик' } });
  fireEvent.click(screen.getByRole('button', { name: copy.plan.title }));
  expect((await screen.findByRole('alert')).textContent).toContain(copy.launchFlow.innError('ooo'));
  expect(calls()).not.toContain('PUT /api/business/marking');
});

it('черновик другого пользователя заменяется сохранённым на сервере', async () => {
  savedOnServer();
  sessionStorage.setItem('ad-autopilot-setup-draft', JSON.stringify({ user: 2, business: { id: 9, site_url: 'https://other.example/', city: 'Казань',
    month_budget_kop: 1000000, avg_check_kop: null }, counter: 0, goals: [], approved: false, maxStep: 2 }));
  renderStep(2);
  await waitFor(() => expect(JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!)).toMatchObject({ user: 1, business: { id: 7 }, maxStep: 5 }));
  expect((await screen.findByLabelText(copy.setup.accounts.counter) as HTMLSelectElement).value).toBe('12345');
});

it('черновик другого пользователя без бизнеса у вошедшего: первый шаг пустой, счётчик чужого черновика не спрашивается', async () => {
  sessionStorage.setItem('ad-autopilot-setup-draft', JSON.stringify({ user: 2, business: { id: 9, site_url: 'https://other.example/', city: 'Казань',
    month_budget_kop: 1000000, avg_check_kop: 200000 }, counter: 555, goals: [3], approved: false, maxStep: 3 }));
  renderStep(1);
  await waitFor(() => expect(JSON.parse(sessionStorage.getItem('ad-autopilot-setup-draft')!)).toMatchObject({ user: 1, counter: 0, maxStep: 1 }));
  for (const label of [copy.setup.business.website, copy.setup.business.city, copy.setup.business.budget, copy.setup.business.visitPrice]) {
    expect((screen.getByLabelText(label) as HTMLInputElement).value).toBe('');
  }
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes('counter=555'))).toBe(false);
  expect(calls()).not.toContain('GET /api/metrica/goals');
});

it('сбой загрузки сохранённого не открывает пустой первый шаг: ошибка с «Повторить», потом шаги с сервера', async () => {
  savedOnServer();
  const saved = fetchMock.getMockImplementation()!;
  let failed = false;
  fetchMock.mockImplementation(async (url, init) => {
    if (new URL(String(url)).pathname === '/api/business' && !failed) {
      failed = true;
      return new Response(JSON.stringify({ detail: 'Сервер недоступен.' }), { status: 503, headers: { 'Content-Type': 'application/json' } });
    }
    return saved(url, init);
  });
  renderStep(1);
  expect((await screen.findByRole('alert')).textContent).toContain('Сервер недоступен.');
  expect(screen.queryByLabelText(copy.setup.business.website)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: copy.common.retry }));
  expect((await screen.findByLabelText(copy.setup.business.city) as HTMLInputElement).value).toBe('Самара');
  expect(calls()).not.toContain('POST /api/business');
});

it('под полем услуг подсказка, что их можно перечислить через запятую', async () => {
  conflictOn('/api/business/description', busy);
  renderStep(4);
  const services = await screen.findByLabelText(copy.setup.draft.services);
  expect(services.getAttribute('aria-describedby')).toBe('services-hint');
  expect(document.getElementById('services-hint')!.textContent).toBe(copy.setup.draft.servicesHint);
});

let directStatus = 'ok';
function accountsStep(counters: { id: number; name: string }[], check: unknown) {
  directStatus = 'ok';
  sessionStorage.setItem('ad-autopilot-setup-draft', JSON.stringify({
    user: 1, business: { id: 7, site_url: 'https://salon.example/', city: 'Москва', month_budget_kop: 3000000, avg_check_kop: null },
    counter: 0, goals: [], approved: false, maxStep: 2,
  }));
  fetchMock.mockImplementation(async (url) => {
    const path = new URL(String(url)).pathname;
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false, vk_oauth: false },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: true,
        accounts: [{ id: 1, business_id: 7, platform: 'direct', external_login: 'owner', status: directStatus, scopes: [] }] },
      '/api/metrica/counters': { items: counters },
      '/api/metrica/goals': { items: [{ id: 1, name: 'Заявка' }] },
      '/api/accounts/check': check,
      '/api/accounts/1': { stopped: true, message: 'Кабинет отключён.' },
    };
    return new Response(JSON.stringify(answers[path] ?? {}), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  renderStep(2);
}

it('пустой список счётчиков объясняет, что их нет на аккаунте, и как создать', async () => {
  accountsStep([], { items: [] });
  expect(await screen.findByText(copy.launchFlow.noCounters, { selector: 'strong' })).not.toBeNull();
  expect(screen.getByText(copy.launchFlow.createCounter)).not.toBeNull();
  expect(screen.getByRole('option').textContent).toBe(copy.launchFlow.noCounters);
});

it('«Проверить подключение» спрашивает площадки и показывает итог текстом, не перечитывая сессию', async () => {
  let answer!: (value: Response) => void;
  accountsStep([{ id: 12345, name: 'Счётчик' }], null);
  const base = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation((url, init) => new URL(String(url)).pathname === '/api/accounts/check'
    ? new Promise<Response>((resolve) => { answer = resolve; }) : base(url, init));
  const button = await screen.findByRole('button', { name: copy.launchFlow.refresh });
  await screen.findByText('Счётчик (12345)');
  fireEvent.click(button);
  await waitFor(() => expect(button.getAttribute('aria-busy')).toBe('true'));
  expect(button.textContent).toBe(copy.launchFlow.checking);
  answer(new Response(JSON.stringify({ items: [
    { platform: 'direct', ok: false, message: 'Яндекс ещё не одобрил доступ к API Директа.' },
    { platform: 'metrica', ok: true, message: 'Метрика отвечает, счётчиков: 1.' },
  ] }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  const failed = await screen.findByText('Яндекс ещё не одобрил доступ к API Директа.', { exact: false });
  expect(failed.closest('li')!.className).toBe('check-failed');
  expect(screen.getByText('Метрика отвечает, счётчиков: 1.', { exact: false })).not.toBeNull();
  expect(button.textContent).toBe(copy.launchFlow.refresh);
  expect(screen.getByText(copy.setup.accounts.directConnected)).not.toBeNull();
  const requests = calls();
  expect(requests).toContain('POST /api/accounts/check');
  expect(requests.filter((call) => call === 'GET /api/me')).toHaveLength(2);
});

it('истёкший доступ после проверки виден на карточке кабинета, а карточка не пропадает на время запроса', async () => {
  let answer!: (value: Response) => void;
  accountsStep([{ id: 12345, name: 'Счётчик' }], { items: [{ platform: 'direct', ok: false, message: 'Доступ истёк — подключите кабинет заново.' }] });
  const button = await screen.findByRole('button', { name: copy.launchFlow.refresh });
  await screen.findByText(copy.setup.accounts.directConnected);
  const base = fetchMock.getMockImplementation()!;
  directStatus = 'needs_reauth';
  fetchMock.mockImplementation((url, init) => new URL(String(url)).pathname === '/api/me'
    ? new Promise<Response>((resolve) => { answer = resolve; }) : base(url, init));
  fireEvent.click(button);
  await screen.findByText('Доступ истёк — подключите кабинет заново.', { exact: false });
  await waitFor(() => expect(calls().filter((call) => call === 'GET /api/me')).toHaveLength(2));
  expect(screen.getByText(copy.setup.accounts.connected('owner'))).not.toBeNull();
  answer(await base('http://localhost/api/me'));
  expect(await screen.findByText(copy.setup.accounts.expired)).not.toBeNull();
  expect(screen.getByText('Доступ истёк — подключите кабинет заново.', { exact: false })).not.toBeNull();
});

it('все площадки ответили — сессию заново не спрашиваем', async () => {
  accountsStep([{ id: 12345, name: 'Счётчик' }], { items: [{ platform: 'direct', ok: true, message: 'Директ отвечает.' }] });
  fireEvent.click(await screen.findByRole('button', { name: copy.launchFlow.refresh }));
  await screen.findByText('Директ отвечает.', { exact: false });
  expect(calls().filter((call) => call === 'GET /api/me')).toHaveLength(1);
});

it('после отключения кабинета прежний итог проверки не показывается', async () => {
  accountsStep([{ id: 12345, name: 'Счётчик' }], { items: [{ platform: 'direct', ok: true, message: 'Директ отвечает.' }] });
  fireEvent.click(await screen.findByRole('button', { name: copy.launchFlow.refresh }));
  await screen.findByText('Директ отвечает.', { exact: false });
  directStatus = 'revoked';
  fireEvent.click(screen.getByRole('button', { name: copy.launchFlow.disconnect }));
  await waitFor(() => expect(screen.queryByText('Директ отвечает.', { exact: false })).toBeNull());
  expect(calls()).toContain('DELETE /api/accounts/1');
});

function budgetHint(hint: unknown) {
  fetchMock.mockImplementation(async (url) => {
    const path = new URL(String(url)).pathname;
    const answers: Record<string, unknown> = {
      '/api/auth/config': { demo: false },
      '/api/me': { id: 1, login: 'owner', email: null, has_business: false,
        accounts: [{ id: 2, business_id: 7, platform: 'vk', external_login: 'owner', status: 'ok', scopes: [] }] },
      '/api/business/budget-hint': hint,
    };
    return new Response(JSON.stringify(answers[path]), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
}
const plain = (text: string) => (_: string, element: Element | null) => element?.tagName === 'P' && element.textContent?.replace(/ /g, ' ') === text;
const vkOff = [{ platform: 'vk', from_kop: 4850000, min_daily_kop: 30000 }];

it('под бюджетом — расчёт сервера: сколько придётся на Директ в день и с какого бюджета подключится ВКонтакте', async () => {
  budgetHint({ minimum_kop: 1270000, daily_kop: { direct: 71225 }, off: vkOff });
  renderStep(1);
  fireEvent.change(await screen.findByLabelText(copy.setup.business.budget), { target: { value: '30000' } });
  expect(await screen.findByText(plain('На Директ придётся около 712 ₽ в день.'))).toBeTruthy();
  expect(screen.getByText(plain('Площадка ВКонтакте подключится от 48 500 ₽ в месяц: ей нужно минимум 300 ₽ в день, мы считаем с запасом.'))).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  expect(calls()).toContain('GET /api/business/budget-hint');
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/api/business/budget-hint?month_budget_kop=3000000&vk=true'))).toBe(true);
});

it('бюджет ниже минимума называет минимум и почему он такой, бюджет от минимума — только расчёт', async () => {
  budgetHint({ minimum_kop: 1270000, daily_kop: { direct: 30100 }, off: vkOff });
  renderStep(1);
  const input = await screen.findByLabelText(copy.setup.business.budget);
  fireEvent.change(input, { target: { value: '12600' } });
  expect((await screen.findByRole('alert')).textContent?.replace(/ /g, ' ')).toBe('Минимальный бюджет — 12 700 ₽ в месяц. '
    + 'Яндекс Директ требует не меньше 300 ₽ на кампанию в неделю, а мы считаем с запасом, чтобы реклама не вставала на паузу в конце недели и месяца.');
  fireEvent.change(input, { target: { value: '12700' } });
  expect(await screen.findByText(plain('На Директ придётся около 301 ₽ в день.'))).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
  expect(screen.queryByRole('status')).toBeNull();
});
