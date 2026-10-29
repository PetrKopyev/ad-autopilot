import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { connectAccount, rublesToKopecks, tooMuchMoney } from './setup';

const fetchMock = vi.fn<typeof fetch>();
const assign = vi.fn();

beforeEach(() => {
  vi.stubGlobal('window', { location: { origin: 'https://app.example', assign } });
  vi.stubGlobal('document', { cookie: '' });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); assign.mockReset(); });

it('в демо подключение кабинета не уводит на сервер, а объясняет, почему нельзя', async () => {
  fetchMock.mockImplementation(async () => Response.json({ demo: true, vk_oauth: false }));
  await expect(connectAccount('direct', 7)).rejects.toThrow(copy.errors.demoConnect);
  await expect(connectAccount('vk', 7)).rejects.toThrow(copy.errors.demoConnect);
  expect(assign).not.toHaveBeenCalled();
});

it('не в демо подключение Директа уходит на вход Яндекса', async () => {
  fetchMock.mockResolvedValue(Response.json({ demo: false, vk_oauth: false }));
  await connectAccount('direct', 7);
  expect(assign).toHaveBeenCalledWith('/api/auth/yandex/start?purpose=direct&business_id=7');
});

it('сумма: запятая и точка — копейки, запятая в конце не ошибка; больше предела — отдельная проверка', () => {
  expect(rublesToKopecks('1500,5')).toBe(150050);
  expect(rublesToKopecks('1500.05')).toBe(150005);
  expect(rublesToKopecks('1500,')).toBe(150000);
  expect(rublesToKopecks('0')).toBeNull();
  expect(tooMuchMoney(rublesToKopecks('10000000'))).toBe(false);
  expect(tooMuchMoney(rublesToKopecks('10000000,01'))).toBe(true);
});
