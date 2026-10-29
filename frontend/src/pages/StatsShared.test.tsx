// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { copy } from '../copy';
import { getAuthConfig } from '../api/setup';
import { Sources } from './StatsShared';

vi.mock('../api/setup', () => ({ getAuthConfig: vi.fn() }));
afterEach(cleanup);

const sources = [{ platform: 'direct', fetched_at: '2026-10-29T10:00:00+03:00' }];

it('в демо вместо «Сверить с кабинетом» — объяснение: кабинета, с которым сверяться, нет', async () => {
  vi.mocked(getAuthConfig).mockResolvedValue({ demo: true, vk_oauth: false });
  render(<Sources sources={sources} />);
  expect(await screen.findByText(copy.screens.compareDemo)).toBeTruthy();
  expect(screen.queryByRole('link', { name: copy.screens.compareDirect })).toBeNull();
});

it('вне демо — ссылка в кабинет Директа', async () => {
  vi.mocked(getAuthConfig).mockResolvedValue({ demo: false, vk_oauth: false });
  render(<Sources sources={sources} />);
  await vi.waitFor(() => expect(getAuthConfig).toHaveBeenCalled());
  expect(screen.getByRole('link', { name: copy.screens.compareDirect })).toBeTruthy();
  expect(screen.queryByText(copy.screens.compareDemo)).toBeNull();
});
