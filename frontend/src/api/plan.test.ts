import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { uploadPhoto } from './plan';

const fetchMock = vi.fn<typeof fetch>();
const photo = new File(['x'], 'photo.webp', { type: 'image/webp' });

beforeEach(() => {
  vi.stubGlobal('window', { location: { origin: 'https://app.example' } });
  vi.stubGlobal('document', { cookie: '' });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset(); });

it('загрузка показывает текст сервера', async () => {
  fetchMock.mockResolvedValueOnce(Response.json({ detail: 'Файл слишком большой: не больше 10 МБ.' }, { status: 413 }));
  await expect(uploadPhoto(1, photo)).rejects.toThrow('Файл слишком большой: не больше 10 МБ.');
});

it('без текста сервера — общая фраза загрузки', async () => {
  fetchMock.mockResolvedValueOnce(new Response('', { status: 500 }));
  await expect(uploadPhoto(1, photo)).rejects.toThrow(copy.launchFlow.uploadError);
});

it('неподдерживаемый формат не отправляем', async () => {
  await expect(uploadPhoto(1, new File(['x'], 'photo.gif', { type: 'image/gif' }))).rejects.toThrow(copy.launchFlow.photoError);
  expect(fetchMock).not.toHaveBeenCalled();
});
