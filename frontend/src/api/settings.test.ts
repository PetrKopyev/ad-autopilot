import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { saveVkGoal } from './settings';

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal('window', { location: { origin: 'https://app.example' } });
  vi.stubGlobal('document', { cookie: '' });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); });

it('цель ВКонтакте уходит вместе с сохранёнными «продолжать» и средним чеком, а не с значениями формы', async () => {
  const stored = { auto_continue: false, avg_check_kop: 250000 };
  fetchMock.mockImplementation(async (_url, init) => Response.json(init?.method === 'PUT'
    ? { ...stored, vk_priced_goal_source_id: 4242, vk_priced_goal_name: 'Заявка' } : stored));
  const result = await saveVkGoal(4242, 'Заявка');
  const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT');
  expect(String(put?.[0])).toMatch(/\/api\/business\/settings$/);
  expect(JSON.parse(String(put?.[1]?.body))).toEqual({ ...stored, vk_priced_goal_source_id: 4242, vk_priced_goal_name: 'Заявка' });
  expect(result.vk_priced_goal_source_id).toBe(4242);
});
